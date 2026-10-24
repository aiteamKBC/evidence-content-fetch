"""Resolve unavailable grid signatures using existing document versions only."""
import copy
import hashlib
import io
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

from django.conf import settings
from pypdf import PdfReader

from .review_api_sync import api_get
from .review_signatures import ENDPOINTS, ROLES, SignatureClient, SignatureError

ENDPOINTS['documents'] = '/MWS.eDocuments/DynamicDocuments/ReadComplianceDocuments'


def signature_blocks(text):
    # Page footers can split a single signature block across two pages.
    text = re.sub(r'(?m)^\s*Review\s*\n\s*Page\s+\d+\s*$', '', text)
    headings = list(re.finditer(r'(?m)^\s*(Advisor|Employer|Participant|Referrer)\s*$', text))
    blocks = {}
    for i, heading in enumerate(headings):
        block = text[heading.end():headings[i+1].start() if i+1 < len(headings) else len(text)]
        if not all(re.search(r'(?m)^\s*' + label + r':', block) for label in ('Name', 'Signature', 'Date')):
            continue
        prefix = heading.group(1)
        if prefix in blocks:
            raise SignatureError('Multiple signature blocks for the same role')
        name = re.search(r'Name:\s*(.*?)\s*(?:Position|Signature):', block, re.S)
        date = re.search(r'Date:\s*(\d{2}/\d{2}/\d{4})', block)
        blocks[prefix] = {'name': ' '.join(name.group(1).split()) if name else '',
                          'date': date.group(1) if date else None}
    return blocks


def classify_pdf(previous, meta, groups, document, text, digest, checked_at):
    result = copy.deepcopy(previous)
    result.update(source='aptem_latest_document_pdf', checked_at=checked_at,
                  verified=False, state='unknown', missing_roles=[], signed_roles=[],
                  document_id=document['Id'], document_sha256=digest,
                  document_created_at=document.get('CreateDateTime'),
                  source_review_status=meta.get('Status'),
                  reason='pdf_signature_verification_incomplete')
    result['roles'] = {role: {'status': 'unknown', 'required': None} for role in ROLES}
    blocks = signature_blocks(text)
    # Multiple programme groups may describe one review following a transfer.
    # Require agreement about the actual signers; ignore differing edit permissions.
    signatures = {tuple(g.get(p+'SignerId') for p in ROLES.values()) for g in groups}
    if not groups or len(signatures) != 1:
        result['reason'] = 'document_group_signers_unavailable_or_conflicting'
        return result
    group = groups[-1]
    if not blocks:
        if all(g.get('ShouldSign') is False and g.get('RequiredSigner') == 0 for g in groups):
            result.update(state='not_required', verified=True, reason='pdf_has_no_signature_fields')
            result['roles'] = {role: {'status': 'not_required', 'required': False} for role in ROLES}
        elif (document.get('IsUploaded') is False and len(text.strip()) > 100
                and all(g.get('ShouldSign') is False and g.get('RequiredSigner') is None
                        and all(g.get(p+'SignerId') is None for p in ROLES.values()) for g in groups)):
            # A deleted historical template can have no requirement configuration.
            # Record the observed document, without inventing current requirements.
            result.update(state='no_signature_fields', verified=True,
                          reason='generated_pdf_has_no_signature_fields_and_template_requirements_unavailable')
            result['roles'] = {role: {'status':'not_present_in_document','required':None} for role in ROLES}
        return result
    roles = {}
    for role, prefix in ROLES.items():
        signer = group.get(prefix+'SignerId')
        block = blocks.get(prefix)
        if not block:
            if signer is not None:
                return result
            roles[role] = {'status': 'not_required', 'required': False}
            continue
        if signer and block['name'] and block['date']:
            state = 'signed'
        elif signer is None and not block['name'] and not block['date']:
            state = 'missing'
        else:
            result['reason'] = 'pdf_and_group_signature_conflict'
            return result
        contact = (previous.get('roles', {}).get(role, {}).get('follow_up_contact') or
                   {'aptem_id': None, 'name': None, 'basis': 'not_available'})
        if role == 'coach':
            contact = {'aptem_id': meta.get('OwnerId'), 'name': meta.get('OwnerName'), 'basis': 'current_review_owner'}
        elif role == 'learner':
            contact = {'aptem_id': meta['LearnerId'], 'name': meta.get('LearnerName'), 'basis': 'learner'}
        roles[role] = {'status': state, 'required': True, 'signed_by_id': signer,
            'signed_by_name': block['name'] or None,
            'signed_at': datetime.strptime(block['date'], '%d/%m/%Y').date().isoformat() if block['date'] else None,
            'signed_at_precision': 'date', 'follow_up_contact': contact}
    missing = [r for r, v in roles.items() if v['status'] == 'missing']
    if missing and any(g.get('IsFullySigned') is True for g in groups):
        result['reason'] = 'pdf_and_group_completion_conflict'
        return result
    result.update(roles=roles, state='missing' if missing else 'signed', verified=True,
                  missing_roles=missing, signed_roles=[r for r,v in roles.items() if v['status']=='signed'])
    result.pop('reason', None)
    return result


class CompletionClient(SignatureClient):
    def metadata(self, review_id):
        raw = api_get(self.session, '/api/MWS.Review/Review/' + str(review_id), timeout=45).json()
        if str(raw.get('id')) != str(review_id):
            raise SignatureError('Review identity mismatch')
        return {'Id': raw['id'], 'LearnerId': raw['userId'], 'OwnerId': raw.get('ownerId'),
                'Status': raw.get('status'), 'raw': raw}

    def documents(self, learner_id, review_id):
        return self.rows('documents', 'Id', userId=learner_id, itemId=review_id, itemType=9)

    def download(self, document_id):
        base = settings.APTEM_BASE_URL.rstrip('/')
        response = self.session.get(base + '/MWS.eDocuments/DynamicDocuments/GetFile',
            params={'documentId': document_id}, timeout=60, allow_redirects=False)
        if response.status_code != 302:
            raise SignatureError('Existing PDF download redirect unavailable')
        target = urljoin(base, response.headers.get('Location', ''))
        parsed = urlsplit(target)
        if (parsed.scheme != 'https' or parsed.netloc != urlsplit(base).netloc
                or not parsed.path.startswith(f'/DownloadFile/Static/{document_id}/')):
            raise SignatureError('Existing PDF download identity mismatch')
        with self.session.get(target, timeout=60, allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise SignatureError('Existing PDF download unavailable')
            parts, size = [], 0
            for part in response.iter_content(65536):
                size += len(part)
                if size > 25 * 1024 * 1024:
                    raise SignatureError('PDF exceeds capture size limit')
                parts.append(part)
        data = b''.join(parts)
        if not data.startswith(b'%PDF-'):
            raise SignatureError('Source document is not a PDF')
        return data

    def resolve(self, row, meta, group_programs, folder):
        if meta is None:
            meta = self.metadata(row['aptem_review_id'])
        if str(meta['LearnerId']) != row['aptem_learner_id'] or str(meta['Id']) != row['aptem_review_id']:
            raise SignatureError('Live review belongs to another learner')
        meta = {**meta, 'LearnerName': row['learner_name']}
        docs = self.documents(row['aptem_learner_id'], row['aptem_review_id'])
        checked = datetime.now(timezone.utc).isoformat()
        result = copy.deepcopy(row['before'])
        result.update(checked_at=checked, source_review_status=meta.get('Status'),
                      source='aptem_direct_document_inventory', document_count=len(docs))
        if not docs:
            result.update(state='document_not_generated', verified=True, reason='direct_document_inventory_empty',
                          missing_roles=[], signed_roles=[], roles={role: {'status':'unknown','required':None} for role in ROLES})
            return {'after': result, 'metadata': meta, 'documents': docs}
        latest = max(docs, key=lambda d: (d.get('VersionCreatedDateTime') or d.get('CreateDateTime') or '', int(d['Id'])))
        groups = []
        for program in group_programs:
            groups.extend(g for g in self.rows('groups', 'ItemId', userId=row['aptem_learner_id'], programId=program)
                          if str(g.get('ItemId')) == row['aptem_review_id'])
        if not groups:
            for episode in self.rows('episodes', 'ProgramId', userId=row['aptem_learner_id']):
                if episode.get('UserId') != int(row['aptem_learner_id']):
                    raise SignatureError('Programme learner mismatch')
                if episode['ProgramId'] in group_programs:
                    continue
                groups.extend(g for g in self.rows('groups','ItemId',userId=row['aptem_learner_id'],programId=episode['ProgramId'])
                              if str(g.get('ItemId')) == row['aptem_review_id'])
        content = self.download(latest['Id'])
        pdf = folder / 'pdfs' / f'{row["aptem_review_id"]}-{latest["Id"]}.pdf'
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(content)
        reader = PdfReader(io.BytesIO(content))
        text = '\n'.join(page.extract_text() or '' for page in reader.pages)
        (pdf.with_suffix('.txt')).write_text(text, encoding='utf-8')
        after_docs = self.documents(row['aptem_learner_id'], row['aptem_review_id'])
        if docs != after_docs:
            raise SignatureError('Document changed during signature verification; retry')
        result = classify_pdf(result, meta, groups, latest, text, hashlib.sha256(content).hexdigest(), checked)
        result['document_count'] = len(docs)
        if 'employer' in result['missing_roles']:
            information = api_get(self.session, '/api/MWS.Review/LearnerInformation/' + row['aptem_review_id'], timeout=45).json()
            name = information.get('manager') or information.get('mentor')
            if name:
                result['roles']['employer']['follow_up_contact'] = {
                    'aptem_id': None, 'name': name, 'basis': 'current_review_learner_information_manager'}
        return {'after': result, 'metadata': meta, 'documents': docs, 'groups': groups}
