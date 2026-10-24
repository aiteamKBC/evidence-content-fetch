"""Read-only Aptem API capture and lossless Neon review upserts."""
import copy
import hashlib
import json
import random
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from uuid import UUID

import requests
from django.conf import settings
from django.db import connection, transaction

from .aptem_auth import AptemLoginError, login_to_aptem


CAPTURE_VERSION = 6
STARTED = {'Completed', 'InProgress', 'AwaitingSignature', 'Archived'}

# Aptem intermittently returns these for a request that would succeed on retry.
TRANSIENT_HTTP = (408, 429, 500, 502, 503, 504)
# How hard a single HTTP call retries a transient response before giving up.
# Tunable via settings; defaults chosen to ride out short Aptem 5xx windows.
HTTP_MAX_ATTEMPTS = max(int(getattr(settings, 'APTEM_HTTP_MAX_ATTEMPTS', 4)), 1)
HTTP_BACKOFF_CAP = float(getattr(settings, 'APTEM_HTTP_BACKOFF_CAP_SECONDS', 30))


def _is_transient(error):
    return isinstance(error, ReviewSyncError) and error.status_code in TRANSIENT_HTTP


def _sleep_backoff(attempt):
    # Capped exponential backoff with jitter so parallel workers do not retry in
    # lockstep and hammer a struggling Aptem endpoint.
    delay = min(HTTP_BACKOFF_CAP, 2.0 ** attempt) + random.uniform(0, 0.5)
    time.sleep(delay)
DISPLAY_STATUS = {
    'NotScheduled': 'Not Scheduled',
    'InProgress': 'In Progress',
}
ACTION_ENDPOINT = {
    'LearnerInformationContent': 'LearnerInformation',
    'MeetingDetailsContent': 'ReviewMeeting',
    'CustomContent': 'Custom',
    'CustomQuestionnaireContent': 'CustomQuestionnaire',
    'CustomTextContent': 'CustomText',
    'AttachmentsContent': 'Attachments',
    'CommentsContent': 'Comments',
    'CoreTopicsContent': 'CoreTopics/All',
    'RagContent': 'Rag',
    'IAGContent': 'IAG',
    'FunctionalSkillsContent': 'FunctionalSkills',
    'LearningProgressContent': 'LearningProgress',
    'ShortAndLongTermGoalsContent': 'ShortAndLongTermGoals/All',
    'DestinationAndProgressContent': 'DestinationAndProgress',
    'EpaGatewayContent': 'EpaGateway',
    'IndividualSupportNeedsContent': 'IndividualSupportNeeds',
    'CurrentJobAndRoleContent': 'CurrentJobAndRole',
    'LearnerSatisfactionContent': 'LearnerSatisfaction',
    'ProgramStatusSelectorContent': 'ProgramStatusSelector',
    'WorkplaceInductionChecklistContent': 'WorkplaceInductionChecklist',
    'PlanReviewsContent': 'PlanReviews',
    'LearningStylesQuestionnaireContent': 'LearningStylesQuestionnaire',
    'PriorLearningContent': 'PriorLearning',
}
METADATA_KEYS = {
    'reviewid', 'id', 'entityid', 'entityname', 'createddate', 'updateddate',
    'userid', 'ownerid', 'programid', 'taskid', 'photourl',
    'isreadonly', 'isonapprenticeshipstandard', 'typeid', 'typeguid', 'typename',
    'lpcomponentid', 'associatedtaskid', 'updateuserid', 'updatedatetime',
    'starteddatetime', 'completedatetime', 'notifydays', 'isprepopulatable',
    'employersectionspermissions', 'referrersectionspermissions',
    'participantsectionspermissions', 'ownername',
}
FLAT_LABELS = {
    'fullName': 'Name', 'programName': 'Programme',
    'programStatus': 'Programme Status', 'programStartDate': 'Programme Start Date',
    'programPlannedEndDate': 'Programme Planned End Date',
    'learningPlannedEndDate': 'Learning Planned End Date',
    'learningActualEndDate': 'Learning Actual End Date',
    'apprenticeshipEndDate': 'Apprenticeship End Date', 'employer': 'Employer',
    'manager': 'Manager', 'mentor': 'Mentor', 'level': 'RAG Level',
    'comment': 'Comment', 'comments': 'Comments',
}
PROGRAM_STATUS = {1: 'Not Started', 2: 'Withdrawn', 3: 'Active',
                  4: 'Completed', 5: 'On Hold', 6: 'Paused'}


class ReviewSyncError(RuntimeError):
    def __init__(self, message, *, status_code=None, source_payload=None, diagnostics=None):
        super().__init__(message)
        self.status_code = status_code
        self.source_payload = source_payload
        self.diagnostics = diagnostics or {}


def _http_error(path, method, status_code, attempt, *, note=None,
                reauthentication=None, source_payload=None):
    # Never retain query values, response bodies, headers, or login exceptions in
    # diagnostic metadata. Original source payloads have their separate contract.
    safe_path = urlsplit(path).path
    diagnostics = {'method': method, 'path': safe_path, 'status_code': status_code,
                   'attempts': attempt}
    if reauthentication:
        diagnostics['reauthentication'] = reauthentication
    message = f'Aptem HTTP {status_code} for {safe_path}'
    if note:
        message += '; ' + note
    return ReviewSyncError(message, status_code=status_code,
                           source_payload=source_payload, diagnostics=diagnostics)


def _capture_failures(exc, component=None):
    diagnostics = getattr(exc, 'diagnostics', None)
    if not diagnostics:
        return []
    failure = dict(diagnostics)
    if component:
        failure['component'] = component
    return [failure]


def display_status(value):
    return DISPLAY_STATUS.get(value, value or '')


def normalized(value):
    return re.sub(r'[^a-z0-9]+', ' ', (value or '').casefold()).strip()


def login_session():
    session = requests.Session()
    try:
        result = login_to_aptem(settings.APTEM_LOGIN_EMAIL, settings.APTEM_LOGIN_PASSWORD,
                                session=session)
    except AptemLoginError:
        session.close()
        raise ReviewSyncError('Aptem authentication failed') from None
    if not result.login_success:
        session.close()
        raise ReviewSyncError('Aptem authentication failed')
    return session


def api_get(session, path, *, params=None, timeout=120):
    url = settings.APTEM_BASE_URL.rstrip('/') + path
    last = None
    for attempt in range(HTTP_MAX_ATTEMPTS):
        try:
            response = session.get(url, params=params, timeout=timeout)
            reauthentication = None
            if response.status_code in (401, 403):
                original_status = response.status_code
                try:
                    result = login_to_aptem(settings.APTEM_LOGIN_EMAIL,
                                            settings.APTEM_LOGIN_PASSWORD, session=session)
                except AptemLoginError:
                    raise _http_error(path, 'GET', original_status, attempt + 1,
                                      note='re-authentication failed',
                                      reauthentication='failed') from None
                if not result.login_success:
                    raise _http_error(path, 'GET', original_status, attempt + 1,
                                      note='re-authentication failed',
                                      reauthentication='failed')
                reauthentication = 'succeeded'
                response = session.get(url, params=params, timeout=timeout)
            if response.status_code in TRANSIENT_HTTP:
                last = _http_error(path, 'GET', response.status_code, attempt + 1,
                                   note='temporary response', reauthentication=reauthentication)
                if attempt + 1 < HTTP_MAX_ATTEMPTS:
                    _sleep_backoff(attempt)
                continue
            if not response.ok:
                try:
                    failure_payload = response.json()
                except ValueError:
                    failure_payload = None
                raise _http_error(path, 'GET', response.status_code, attempt + 1,
                                  reauthentication=reauthentication, source_payload=failure_payload)
            return response
        except ReviewSyncError:
            raise
        except requests.RequestException as exc:
            last = ReviewSyncError('Aptem request failed: '+type(exc).__name__)
            if attempt + 1 < HTTP_MAX_ATTEMPTS:
                _sleep_backoff(attempt)
    raise last or ReviewSyncError('Aptem request failed')


def _review_type_light_read(session, params):
    """The documented review-type READ query uses POST with an empty body."""
    path = '/MWS.Review/Review/LightRead'
    url = settings.APTEM_BASE_URL.rstrip('/') + path
    last = None
    for attempt in range(3):
        try:
            response = session.post(
                url, params=params, data=None,
                headers={'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest'},
                timeout=60, allow_redirects=False)
            reauthentication = None
            if response.status_code in (401, 403):
                original_status = response.status_code
                try:
                    result = login_to_aptem(settings.APTEM_LOGIN_EMAIL,
                                            settings.APTEM_LOGIN_PASSWORD, session=session)
                except AptemLoginError:
                    raise _http_error(path, 'POST', original_status, attempt + 1,
                                      note='re-authentication failed',
                                      reauthentication='failed') from None
                if not result.login_success:
                    raise _http_error(path, 'POST', original_status, attempt + 1,
                                      note='re-authentication failed',
                                      reauthentication='failed')
                reauthentication = 'succeeded'
                response = session.post(
                    url, params=params, data=None,
                    headers={'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest'},
                    timeout=60, allow_redirects=False)
            if response.status_code in (408, 429, 500, 502, 503, 504):
                last = _http_error(path, 'POST', response.status_code, attempt + 1,
                                   note='temporary response', reauthentication=reauthentication)
                time.sleep(2 ** attempt)
                continue
            if response.status_code != 200:
                raise _http_error(path, 'POST', response.status_code, attempt + 1,
                                  reauthentication=reauthentication)
            return response.json()
        except requests.RequestException as exc:
            last = ReviewSyncError('Aptem type read query failed: ' + type(exc).__name__)
            time.sleep(2 ** attempt)
    raise last or ReviewSyncError('Aptem type read query failed')


def _recover_review_type_guid(session, review_id, target):
    """Recover a missing GUID from the target's own learner/programme/type ID."""
    proof = {'method': 'POST', 'endpoint': '/MWS.Review/Review/LightRead',
             'request_body': None, 'verified': False,
             'source': 'target_review_numeric_type_id_mapping'}
    try:
        source = api_get(session, '/odata/1.0/Reviews', params={
            '$filter': f'Id eq {int(review_id)}', '$select': 'Id,LearnerId,ProgramId',
        }).json()
        proof['review_inventory_response'] = source
        rows = source.get('value') if isinstance(source, dict) else None
        if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
                or str(rows[0].get('Id')) != str(review_id)
                or str(rows[0].get('LearnerId')) != str(target.get('userId'))
                or rows[0].get('ProgramId') in (None, '')):
            raise ReviewSyncError('Type GUID target learner/programme identity is invalid')
        type_id = target.get('typeId')
        if not isinstance(type_id, int) or isinstance(type_id, bool):
            raise ReviewSyncError('Type GUID recovery requires an exact numeric type ID')
        params = {'userId': int(rows[0]['LearnerId']), 'programId': int(rows[0]['ProgramId']),
                  'typeGuidToBeIncluded': ''}
        proof['params'] = params
        proof['target_type_id'] = type_id
        response = _review_type_light_read(session, params)
        proof['response'] = response
        if not isinstance(response, dict) or not isinstance(response.get('Data'), list):
            raise ReviewSyncError('Review type read query has an invalid response')
        if not _empty(response.get('Errors')):
            raise ReviewSyncError('Review type read query returned source errors')
        matches = [row for row in response['Data'] if isinstance(row, dict)
                   and isinstance(row.get('Id'), int) and not isinstance(row['Id'], bool)
                   and row['Id'] == type_id]
        if len(matches) != 1:
            raise ReviewSyncError('Review type read query has no unique exact type ID match')
        raw_guid = matches[0].get('Guid')
        if not isinstance(raw_guid, str) or not raw_guid:
            raise ReviewSyncError('Review type read query has no GUID')
        guid = UUID(raw_guid)
        if not guid.int:
            raise ReviewSyncError('Review type read query has an empty GUID')
        proof.update(verified=True, type_guid=str(guid), selected_type=matches[0])
        return str(guid), proof
    except (ReviewSyncError, ValueError, TypeError) as exc:
        proof['error'] = str(exc)
        if failures := _capture_failures(exc):
            proof['capture_failures'] = failures
        return None, proof


def live_inventory(session, *, review_ids=None):
    path = '/odata/1.0/Reviews'
    params = {'$count': 'true'}
    requested = None
    if review_ids is not None:
        requested = {str(value) for value in review_ids}
        if (not requested or len(requested) > 200
                or any(not re.fullmatch(r'[1-9][0-9]*', value) for value in requested)):
            raise ReviewSyncError('Selected inventory requires 1..200 positive numeric review IDs')
        params['$filter'] = ' or '.join(
            f'Id eq {value}' for value in sorted(requested, key=int))
    rows, visited = [], set()
    expected_count = None
    origin = urlsplit(settings.APTEM_BASE_URL)
    while path:
        if path in visited:
            raise ReviewSyncError('Aptem review inventory pagination loop')
        visited.add(path)
        data = api_get(session, path, params=params,
                       timeout=120 if requested is not None else 600).json()
        if not isinstance(data, dict) or not isinstance(data.get('value'), list):
            raise ReviewSyncError('Aptem review inventory page is invalid')
        rows.extend(data['value'])
        count = data.get('@odata.count', data.get('odata.count'))
        if count is not None:
            try:
                count = int(count)
            except (ValueError, TypeError) as exc:
                raise ReviewSyncError('Aptem review inventory count is invalid') from exc
            if expected_count is not None and count != expected_count:
                raise ReviewSyncError('Aptem review inventory changed during pagination')
            expected_count = count
        next_link = data.get('@odata.nextLink') or data.get('odata.nextLink')
        if not next_link:
            break
        absolute = urlsplit(urljoin(settings.APTEM_BASE_URL.rstrip('/') + path, next_link))
        if (absolute.scheme, absolute.netloc) != (origin.scheme, origin.netloc):
            raise ReviewSyncError('Aptem review pagination points outside the Aptem origin')
        if absolute.path.rstrip('/') != '/odata/1.0/Reviews':
            raise ReviewSyncError('Aptem review pagination points outside the review inventory')
        path = absolute.path + ('?' + absolute.query if absolute.query else '')
        params = None
    if not rows:
        raise ReviewSyncError('Aptem review inventory is empty or invalid')
    if expected_count is not None and len(rows) != expected_count:
        raise ReviewSyncError('Aptem review inventory does not match the advertised count')
    if any(not isinstance(row, dict) or row.get('LearnerId') in (None, '') for row in rows):
        raise ReviewSyncError('Aptem review inventory contains invalid learner identities')
    ids = [str(row.get('Id')) for row in rows]
    if any(value in ('', 'None') for value in ids) or len(ids) != len(set(ids)):
        raise ReviewSyncError('Aptem review inventory contains missing or duplicate IDs')
    if requested is not None and set(ids) != requested:
        raise ReviewSyncError('Aptem selected inventory does not match the requested review IDs')
    return rows


def _answers_of(payload):
    if not isinstance(payload, dict):
        return {}
    answers = {}
    fields = payload.get('fields')
    if isinstance(fields, list) and fields:
        for field in fields:
            if not isinstance(field, dict):
                continue
            name = field.get('name')
            title = field.get('title') or name or ''
            value = payload.get(name) if name else None
            # listAnswers describes selectable options, not selected answers.
            # It remains available in source_payload without inventing a value.
            if not _empty(value):
                _add_answer(answers, title, value)
        return answers
    for key, value in payload.items():
        if key not in ('entityName', 'entityId', 'fields') and not _empty(value):
            answers[key] = value
    return answers


def _empty(value):
    # False and zero are answers, not missing values.
    return value is None or value == '' or value == [] or value == {}


def _add_answer(answers, label, value):
    label = str(label or 'Answer')
    candidate, occurrence = label, 2
    while candidate in answers:
        candidate = f'{label} [{occurrence}]'
        occurrence += 1
    answers[candidate] = value


def real_answers(payload):
    if isinstance(payload, list):
        combined = {}
        for index, item in enumerate(payload, 1):
            for key, value in real_answers(item).items():
                combined[f'Item {index} - {key}'] = value
        return combined
    if not isinstance(payload, dict):
        return {} if _empty(payload) else {'Value': payload}
    answers = {}
    declared_fields = isinstance(payload.get('fields'), list) and bool(payload['fields'])
    for key, value in _answers_of(payload).items():
        if not declared_fields and key.strip().lower().replace(' ', '') in METADATA_KEYS:
            continue
        if key == 'programStatus' and isinstance(value, int) and not isinstance(value, bool):
            value = PROGRAM_STATUS.get(value, value)
        _add_answer(answers, FLAT_LABELS.get(key, key), value)
    return answers


def _text(value):
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _section_payload(label, answers, *, source_payload=None, source_action=None,
                     source_status=None, capture_state='verified', capture_error=None,
                     capture_failures=None):
    fields = [{'label': key, 'value': _text(value)} for key, value in answers.items()]
    raw = '\n\n'.join(f'{key}\n{_text(value)}' for key, value in answers.items()).strip()
    result = {'section_name': label, 'fields': fields, 'tables': [], 'raw_text': raw,
              'source_payload': source_payload, 'source_action': source_action,
              'source_status': source_status, 'capture_state': capture_state}
    if capture_error:
        result['capture_error'] = capture_error
    if capture_failures:
        result['capture_failures'] = capture_failures
    return result


def _fetch_skill_radar(session, manifest, label):
    """Capture the same learner competency report embedded by Aptem reviews."""
    user_id = manifest.get('userId')
    if user_id in (None, ''):
        raise ReviewSyncError('Skill Radar review has no Aptem learner ID')
    competencies = api_get(
        session, '/api/MWS.LM/Competency/AssignedCompetencies',
        params={'userId': str(user_id)},
    ).json()
    if not isinstance(competencies, list):
        raise ReviewSyncError('Aptem Skill Radar competency list is invalid')
    captured = []
    for competency in competencies:
        competency_id = competency.get('id') if isinstance(competency, dict) else None
        if competency_id is None:
            raise ReviewSyncError('Aptem Skill Radar competency has no ID')
        characteristics = api_get(
            session, '/api/MWS.LM/Competency/Progress',
            params={'competencyId': str(competency_id), 'userId': str(user_id)},
        ).json()
        if not isinstance(characteristics, list):
            raise ReviewSyncError('Aptem Skill Radar progress is invalid')
        captured.append({'competency': competency, 'characteristics': characteristics})
    # Keep the complete API objects in JSON fields. This is deliberately more
    # lossless than recreating the chart percentages shown by Aptem's UI.
    answers = {
        f'Competency {index} - {(item["competency"].get("name") or item["competency"].get("id"))}': item
        for index, item in enumerate(captured, 1)
    }
    result = _section_payload(label, answers, source_payload={
        'assigned_competencies': competencies, 'competency_progress': captured})
    result['source_scope'] = 'current_learner_competency_report'
    return result


def _fetch_section(session, review_id, section, manifest):
    action = (section.get('contentRoute') or {}).get('action')
    endpoint = ACTION_ENDPOINT.get(action)
    label = (section.get('customName') or section.get('name') or '').strip()
    if not label:
        raise ReviewSyncError('Aptem review contains an unnamed section')
    if action == 'SkillRadarContent':
        captured = _fetch_skill_radar(session, manifest, label)
        captured.update(source_action=action, source_status=section.get('status'))
        return captured
    if action == 'ActionsContent':
        return _fetch_actions(session, review_id, section, manifest, label)
    if action == 'PLRContent':
        return _fetch_plr(session, section, manifest, label)
    if not endpoint:
        raise ReviewSyncError(f'Unmapped Aptem review section action: {action or "missing"}')
    params = None
    extended = None
    path = f'/api/MWS.Review/{endpoint}/{review_id}'
    if endpoint in ('Custom', 'CustomQuestionnaire', 'CustomText'):
        params = {'customName': section.get('customName') or ''}
    elif endpoint == 'ReviewMeeting':
        extended = api_get(session, f'/api/MWS.Review/Review/{review_id}/Extended').json()
        _validate_identity(extended, review_id, manifest.get('userId'))
        if not extended.get('associatedTaskId'):
            raise ReviewSyncError('Review meeting has no associated task')
        path = '/api/MWS.Review/ReviewMeeting'
        params = {'taskId': extended['associatedTaskId'], 'userId': extended.get('userId')}
    try:
        response = api_get(session, path, params=params)
    except ReviewSyncError as exc:
        if endpoint == 'ReviewMeeting' and isinstance(extended, dict):
            # Only this exact Aptem response proves the linked meeting is absent.
            # A generic error or an Incomplete label cannot establish emptiness.
            absent = (exc.status_code == 400 and isinstance(exc.source_payload, dict)
                      and exc.source_payload.get('message') == 'There is no corresponding Review meeting')
            captured = _section_payload(
                label, real_answers(extended),
                source_payload={'extended': extended, 'meeting_response': exc.source_payload},
                source_action=action, source_status=section.get('status'),
                capture_state='verified_source_absent' if absent else 'unavailable',
                capture_error=None if absent else str(exc),
                capture_failures=None if absent else _capture_failures(exc))
            captured['source_scope'] = 'review_metadata_only'
            captured['source_endpoint'] = path
            return captured
        raise
    content_type = response.headers.get('content-type', '')
    if 'json' not in content_type.lower():
        raise ReviewSyncError('Aptem review section did not return JSON')
    source_payload = response.json()
    captured = _section_payload(label, real_answers(source_payload),
                               source_payload=source_payload, source_action=action,
                               source_status=section.get('status'))
    captured['source_endpoint'] = path
    return captured


VERIFIED_SECTION_STATES = {'verified', 'verified_source_absent'}


def _fetch_plr(session, section, manifest, label):
    """PLR is a current learner record, distinct from review Prior Learning."""
    user_id = manifest.get('userId')
    if user_id in (None, ''):
        raise ReviewSyncError('PLR section has no Aptem learner identity')
    user_id = str(int(user_id))
    payload, answers, errors, failures = {}, {}, [], []
    endpoints = {
        'learner_identity': '/odata/1.0/Users',
        'qualifications': f'/api/Users/{user_id}/PLR',
        'prior_attainment': f'/api/Users/{user_id}/PLR/PriorAttainmentLevel',
    }
    # Establish the current learner identity and retain the optional ULN before
    # requesting that learner's qualification and attainment records.
    try:
        identity = api_get(session, endpoints['learner_identity'], params={
            '$filter': f'Id eq {user_id}', '$select': 'Id,UserILRSummary_ULN',
        }).json()
        rows = identity.get('value') if isinstance(identity, dict) else None
        if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
                or str(rows[0].get('Id')) != user_id):
            raise ReviewSyncError('PLR learner identity response does not match the review learner')
        payload['learner_identity'] = identity
        if 'UserILRSummary_ULN' not in rows[0]:
            raise ReviewSyncError('PLR learner identity response omitted the requested ULN field')
        if not _empty(rows[0]['UserILRSummary_ULN']):
            answers['ULN'] = rows[0]['UserILRSummary_ULN']
    except (ReviewSyncError, ValueError) as exc:
        errors.append(f'learner_identity: {exc}')
        failures.extend(_capture_failures(exc, 'learner_identity'))
        payload['learner_identity_error'] = getattr(exc, 'source_payload', None)
    if 'learner_identity' in payload:
        for kind in ('qualifications', 'prior_attainment'):
            try:
                source = api_get(session, endpoints[kind]).json()
                if kind == 'qualifications':
                    if (not isinstance(source, list)
                            or any(not isinstance(row, dict) for row in source)):
                        raise ReviewSyncError('PLR qualifications response is not a list of records')
                    if any(str(row.get('userId')) != user_id for row in source):
                        raise ReviewSyncError('PLR qualification belongs to a different learner')
                    payload[kind] = source
                    if source:
                        answers['Qualifications'] = source
                else:
                    if not isinstance(source, dict) or str(source.get('userId')) != user_id:
                        raise ReviewSyncError('PLR prior attainment belongs to a different learner')
                    payload[kind] = source
                    required = {'englishLevel', 'mathsLevel', 'ictLevel', 'selectedHighestLevel',
                                'learnerSelectedHighestLevel', 'isIlrUserFieldsReadonly'}
                    if not required <= source.keys():
                        raise ReviewSyncError('PLR prior attainment response is incomplete')
                    for key, value in real_answers(source).items():
                        answers['Prior attainment - ' + key] = value
            except (ReviewSyncError, ValueError) as exc:
                errors.append(f'{kind}: {exc}')
                failures.extend(_capture_failures(exc, kind))
                payload[kind + '_error'] = getattr(exc, 'source_payload', None)
    captured = _section_payload(
        label, answers, source_payload=payload, source_action='PLRContent',
        source_status=section.get('status'), capture_state='unavailable' if errors else 'verified',
        capture_error='; '.join(errors) if errors else None, capture_failures=failures)
    captured.update(source_scope='current_learner_plr', source_contract='plr-ui-v1',
                    source_learner_id=user_id, source_endpoints=endpoints)
    return captured


def _fetch_actions(session, review_id, section, manifest, label):
    """Capture the documented review snapshot/current programme actions routes."""
    payload, endpoints, error = {}, {}, None
    failures = []
    actions, scope, snapshot_available = [], None, False
    try:
        # First establish the review identity/status. The UI then requests the
        # learner's CURRENT programme, which can differ from review.ProgramId.
        identity = api_get(session, '/odata/1.0/Reviews', params={
            '$filter': f'Id eq {int(review_id)}',
            '$select': 'Id,LearnerId,ProgramId,Status',
        }).json()
        rows = identity.get('value') if isinstance(identity, dict) else None
        payload['review_identity'] = identity
        if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
                or str(rows[0].get('Id')) != str(review_id)
                or str(rows[0].get('LearnerId')) != str(manifest.get('userId'))
                or rows[0].get('ProgramId') in (None, '')):
            raise ReviewSyncError('Actions review/learner/programme identity could not be established')
        review = rows[0]
        learner_data = api_get(session, '/odata/1.0/Users', params={
            '$filter': f'Id eq {int(review["LearnerId"])}',
            '$select': 'Id,UserProgram_ProgramId',
        }).json()
        payload['current_learner_programme'] = learner_data
        users = learner_data.get('value') if isinstance(learner_data, dict) else None
        if (not isinstance(users, list) or len(users) != 1 or not isinstance(users[0], dict)
                or str(users[0].get('Id')) != str(review['LearnerId'])
                or users[0].get('UserProgram_ProgramId') in (None, '')):
            raise ReviewSyncError('Actions current learner programme could not be established')
        current_programme_id = users[0]['UserProgram_ProgramId']
        base = (f'/actions/api/v2/learners/{int(review["LearnerId"])}/'
                f'programmes/{int(current_programme_id)}')
        if review.get('Status') in ('Completed', 'AwaitingSignature'):
            endpoints['snapshot'] = f'{base}/snapshots/{review_id}'
            response = api_get(session, endpoints['snapshot'])
            empty_body = response.status_code == 200 and response.content == b''
            raw_snapshot = None if empty_body else response.json()
            payload['snapshot'] = raw_snapshot
            if empty_body:
                payload['snapshot_response'] = {'http_status': 200, 'empty_body': True}
            decoded_snapshot = raw_snapshot
            if isinstance(raw_snapshot, str):
                decoded_snapshot = json.loads(raw_snapshot) if raw_snapshot.strip() else None
            if decoded_snapshot is not None:
                if not isinstance(decoded_snapshot, list):
                    raise ReviewSyncError('Actions snapshot is not a JSON list')
                actions, snapshot_available = decoded_snapshot, True
                scope = 'review_snapshot'
        if not snapshot_available:
            endpoints['current'] = f'{base}/actions'
            actions = api_get(session, endpoints['current']).json()
            payload['current_actions'] = actions
            scope = ('current_learner_programme_fallback' if 'snapshot' in endpoints
                     else 'current_learner_programme')
        if not isinstance(actions, list) or any(not isinstance(row, dict) for row in actions):
            raise ReviewSyncError('Programme actions response is not a list of action objects')
    except (ReviewSyncError, ValueError, TypeError) as exc:
        error = str(exc)
        failures.extend(_capture_failures(exc))
        payload['source_error'] = getattr(exc, 'source_payload', None)
    captured = _section_payload(
        label, real_answers(actions) if not error else {}, source_payload=payload,
        source_action='ActionsContent', source_status=section.get('status'),
        capture_state='unavailable' if error else 'verified', capture_error=error,
        capture_failures=failures)
    captured.update(source_scope=scope, source_endpoints=endpoints,
                    source_snapshot_available=snapshot_available)
    return captured


def _validate_identity(payload, review_id, learner_id=None):
    if not isinstance(payload, dict):
        raise ReviewSyncError('Aptem review identity payload is invalid')
    for key in ('id', 'Id', 'reviewId'):
        if payload.get(key) is not None and str(payload[key]) != str(review_id):
            raise ReviewSyncError('Aptem response belongs to a different review')
    if (learner_id is not None and payload.get('userId') is not None
            and str(payload['userId']) != str(learner_id)):
        raise ReviewSyncError('Aptem response belongs to a different learner')


def _capture_sections(session, review_id, manifest):
    manifest.setdefault('supplemental_sections', [])
    source_sections = manifest.get('sections')
    if not isinstance(source_sections, list):
        raise ReviewSyncError('Aptem review has no valid section manifest')
    sections = []
    for source in source_sections:
        if not isinstance(source, dict):
            raise ReviewSyncError('Aptem review section manifest contains an invalid row')
        label = (source.get('customName') or source.get('name') or '').strip()
        if not label:
            raise ReviewSyncError('Aptem review contains an unnamed section')
        try:
            captured = _fetch_section(session, review_id, source, manifest)
        except (ReviewSyncError, ValueError) as exc:
            captured = _section_payload(
                label, {}, source_payload=getattr(exc, 'source_payload', None),
                source_action=(source.get('contentRoute') or {}).get('action'),
                source_status=source.get('status'), capture_state='unavailable',
                capture_error=str(exc), capture_failures=_capture_failures(exc))
        sections.append(captured)
    _set_manifest_verification(manifest, sections)
    return sections


def _set_manifest_verification(manifest, sections):
    errors = [{'section_name': section['section_name'],
               'source_action': section.get('source_action'),
               'error': section.get('capture_error', 'Section capture is unverified'),
               **({'capture_failures': section['capture_failures']}
                  if section.get('capture_failures') else {})}
              for section in sections
              if section.get('capture_state') not in VERIFIED_SECTION_STATES]
    manifest['capture_errors'] = errors
    manifest['capture_verified'] = not errors and not manifest.get('manifest_reconstructed')


def _validate_summary(payload, review_id):
    if (not isinstance(payload, dict) or str(payload.get('ReviewId')) != str(review_id)
            or 'SummarisationText' not in payload
            or not isinstance(payload['SummarisationText'], (str, type(None)))):
        raise ReviewSyncError('Review summary response has invalid identity or content')


def _fetch_meeting_summary(session, review_id):
    """The UI's Meeting Summary panel has separate summary and transcript APIs."""
    payload, answers, errors, failures = {}, {}, [], []
    endpoints = {
        'summary': f'/api/Aptem.Review.Summarisation/ReviewSummaries/{review_id}',
        'transcripts': f'/transcripts/api/v2/review/{review_id}',
    }
    for kind, path in endpoints.items():
        try:
            response = api_get(session, path)
            empty_transcript = (kind == 'transcripts' and response.status_code == 200
                                and response.content == b'')
            # Aptem's Angular client treats the documented HTTP 200, zero-byte
            # transcript response as null. Keep its HTTP proof separately.
            source = None if empty_transcript else response.json()
            if empty_transcript:
                payload['transcripts_response'] = {'http_status': 200, 'empty_body': True}
            payload[kind] = source
            if kind == 'summary':
                _validate_summary(source, review_id)
                if not _empty(source['SummarisationText']):
                    answers['Summary'] = source['SummarisationText']
            else:
                # The UI explicitly uses optional chaining for a JSON null
                # transcript record; HTTP errors are never treated as null.
                if source is not None:
                    if (not isinstance(source, dict)
                            or str(source.get('entityId')) != str(review_id)
                            or not isinstance(source.get('files'), list)):
                        raise ReviewSyncError('Review transcript response has invalid identity or files')
                    if not _empty(source.get('meetingNotes')):
                        answers['Meeting Notes'] = source['meetingNotes']
                    if source['files']:
                        answers['Transcripts'] = source['files']
        except (ReviewSyncError, ValueError) as exc:
            errors.append(f'{kind}: {exc}')
            failures.extend(_capture_failures(exc, kind))
            if kind not in payload:
                payload[kind + '_error'] = getattr(exc, 'source_payload', None)
    captured = _section_payload(
        'Meeting Summary', answers, source_payload=payload,
        source_action='ReviewSummarySupplemental', source_status=None,
        capture_state='unavailable' if errors else 'verified',
        capture_error='; '.join(errors) if errors else None, capture_failures=failures)
    captured.update(source_kind='supplemental', source_endpoints=endpoints)
    return captured


def _fetch_previous_meeting_summary(session, review_id, manifest):
    """Match the UI's same-type latest completed review (ID breaks date ties)."""
    payload, answers = {}, {}
    path = f'/api/Aptem.Review.Summarisation/SummarisedReviews/{review_id}/Previous'
    error, failures = None, []
    try:
        reviews = api_get(session, path).json()
        payload['previous_reviews'] = reviews
        if not isinstance(reviews, list) or any(not isinstance(row, dict) for row in reviews):
            raise ReviewSyncError('Previous review summary selector is invalid')
        if reviews and not manifest.get('typeGuid'):
            raise ReviewSyncError('Review type GUID is unavailable for the previous summary selector')
        candidates = [row for row in reviews if row.get('typeGuid') == manifest.get('typeGuid')]
        selected = None
        if candidates:
            def ordering(row):
                try:
                    timestamp = datetime.fromisoformat(row['completeDateTime'].replace('Z', '+00:00'))
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=timezone.utc)
                    return timestamp, int(row['id'])
                except (ValueError, TypeError, KeyError, AttributeError) as exc:
                    raise ReviewSyncError('Previous summary selector has an invalid date or ID') from exc
            selected = max(candidates, key=ordering)
        payload['selected_review_id'] = str(selected['id']) if selected else None
        if selected:
            summary_path = f'/api/Aptem.Review.Summarisation/ReviewSummaries/{selected["id"]}'
            source = api_get(session, summary_path).json()
            payload['summary'] = source
            _validate_summary(source, selected['id'])
            if not _empty(source['SummarisationText']):
                answers['Summary'] = source['SummarisationText']
    except (ReviewSyncError, ValueError) as exc:
        error = str(exc)
        failures.extend(_capture_failures(exc))
        payload['source_error'] = getattr(exc, 'source_payload', None)
    captured = _section_payload(
        'Previous Meeting Summary', answers, source_payload=payload,
        source_action='PreviousReviewSummarySupplemental', source_status='Readonly',
        capture_state='unavailable' if error else 'verified', capture_error=error,
        capture_failures=failures)
    captured.update(source_kind='supplemental', source_endpoint=path,
                    source_scope='ui_selected_previous_review')
    return captured


def _fetch_supplemental_sections(session, review_id, manifest):
    if not any((section.get('contentRoute') or {}).get('action') == 'MeetingDetailsContent'
               for section in manifest['sections']):
        return []
    # These are proven UI panels, loaded outside ReviewContent.sections. Their
    # source descriptors are separate so the original manifest stays intact.
    sections = [_fetch_meeting_summary(session, review_id),
                _fetch_previous_meeting_summary(session, review_id, manifest)]
    manifest['supplemental_sections'] = [
        {key: section[key] for key in ('section_name', 'source_action', 'source_status')}
        for section in sections]
    return sections


def fetch_review_content(session, review_id):
    manifest = api_get(session, f'/api/MWS.Review/ReviewContent/{review_id}').json()
    _validate_identity(manifest, review_id)
    sections = _capture_sections(session, review_id, manifest)
    sections.extend(_fetch_supplemental_sections(session, review_id, manifest))
    _set_manifest_verification(manifest, sections)
    return manifest, sections


def fetch_review_content_from_template(session, review_id, template_review_id):
    """Recover a review whose own manifest is broken using an identical type.

    The template supplies section routes/names only. Every section value is
    fetched using the target review ID, and the internal Aptem type IDs must
    match before any data is returned.
    """
    target = api_get(session, f'/api/MWS.Review/Review/{review_id}/Extended').json()
    _validate_identity(target, review_id)
    template_meta = api_get(
        session, f'/api/MWS.Review/Review/{template_review_id}/Extended').json()
    _validate_identity(template_meta, template_review_id)
    if not target.get('typeId') or target.get('typeId') != template_meta.get('typeId'):
        raise ReviewSyncError('Template review type does not match target review type')
    template = api_get(
        session, f'/api/MWS.Review/ReviewContent/{template_review_id}').json()
    source_sections = template.get('sections')
    if not isinstance(source_sections, list) or not source_sections:
        raise ReviewSyncError('Template review has no section manifest')
    manifest = dict(target)
    manifest.update({
        'sections': copy.deepcopy(source_sections),
        'manifest_reconstructed': True,
        'manifest_template_review_id': str(template_review_id),
        'manifest_reconstruction_reason': 'Target ReviewContent endpoint returned HTTP 500',
    })
    if not manifest.get('typeGuid'):
        type_guid, proof = _recover_review_type_guid(session, review_id, target)
        manifest['type_guid_recovery'] = proof
        if type_guid is not None:
            manifest['typeGuid'] = type_guid
    for source in manifest['sections']:
        # Template section status does not describe the target review.
        source['status'] = 'UnknownFromTemplate'
    sections = _capture_sections(session, review_id, manifest)
    sections.extend(_fetch_supplemental_sections(session, review_id, manifest))
    _set_manifest_verification(manifest, sections)
    return manifest, sections


def canonical_mapping():
    """Map Aptem learner ID to canonical learner; email fallback must be unique."""
    with connection.cursor() as cursor:
        cursor.execute('SELECT id,aptem_id,lower(trim(email)) FROM "Learner".learners')
        learners = cursor.fetchall()
        cursor.execute('SELECT "ID",lower(trim("Email")) FROM "LMS"."Aptem_users"')
        users = cursor.fetchall()
    by_aptem = {}
    for learner_id, aptem_id, _ in learners:
        if aptem_id is None:
            continue
        if str(aptem_id) in by_aptem:
            raise ReviewSyncError('Multiple canonical learners have the same Aptem learner ID')
        by_aptem[str(aptem_id)] = learner_id
    by_email = {}
    for learner_id, _, email in learners:
        if email:
            by_email.setdefault(email, []).append(learner_id)
    user_email = {}
    for aptem_id, email in users:
        if email:
            user_email.setdefault(str(aptem_id), []).append(email)
    mapping = dict(by_aptem)
    fallback = {}
    claimed = set(by_aptem.values())
    email_user_ids = {}
    for aptem_id, emails in user_email.items():
        for email in set(emails):
            email_user_ids.setdefault(email, set()).add(aptem_id)
    for aptem_id, emails in user_email.items():
        if aptem_id in mapping or len(emails) != 1:
            continue
        candidates = by_email.get(emails[0], [])
        if (len(candidates) == 1 and candidates[0] not in claimed
                and len(email_user_ids[emails[0]]) == 1):
            mapping[aptem_id] = candidates[0]
            fallback[aptem_id] = candidates[0]
            claimed.add(candidates[0])
    return mapping, fallback


def stored_inventory():
    with connection.cursor() as cursor:
        cursor.execute('''SELECT r.id,r.aptem_review_id,r.learner_id,r.status,
            r.extraction_status,coalesce(s.section_count,0),
            r.review_data->>'capture_version',r.review_data->>'capture_verified',
            r.review_data->>'content_source_updated_at'
            FROM "Learner".reviews r
            LEFT JOIN (SELECT review_id,count(*) section_count
                       FROM "Learner".review_sections GROUP BY review_id) s ON s.review_id=r.id
            WHERE r.aptem_review_id IS NOT NULL''')
        return {str(row[1]): {'id': row[0], 'learner_id': row[2], 'status': row[3],
            'extraction_status': row[4], 'sections': row[5],
            'capture_version': int(row[6]) if str(row[6] or '').isdigit() else 0,
            'capture_verified': row[7] == 'true', 'content_source_updated_at': row[8]}
            for row in cursor.fetchall()}


def needs_content(review, existing):
    status = review.get('Status')
    if status not in STARTED:
        return False
    if not existing or existing['extraction_status'] != 'success' or not existing['sections']:
        return True
    if (int(existing.get('capture_version') or 0) < CAPTURE_VERSION
            or not existing.get('capture_verified')):
        return True
    if review.get('UpdatedDate') != existing.get('content_source_updated_at'):
        return True
    # Stable reviews may skip repeat capture only after a corrected, verified
    # content capture; a metadata update never upgrades an older capture.
    if status in ('Completed', 'Archived') and existing['status'] == status:
        return False
    return True


def _decoded_json(value, default):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError as exc:
            raise ReviewSyncError('Stored review JSON is invalid; refusing to overwrite it') from exc
    return default if value is None else value


def _payload(review, api_meta=None, sections=None, old_data=None, source_kind='odata'):
    previous = _decoded_json(old_data, {})
    if not isinstance(previous, dict):
        raise ReviewSyncError('Stored review data is not a JSON object')
    data = dict(previous)
    data.update({'aptem_learner_id': str(review['LearnerId']),
                 'aptem_review_id': str(review['Id'])})
    if source_kind == 'classic':
        data['classic_review_extended'] = review['classic_extended']
        data['visible_inventory_source'] = 'classic_ui'
    else:
        data['live_odata'] = review
    if api_meta is not None:
        # An earlier failed audit must not contradict the current capture.
        # The new manifest/section verification below is authoritative for
        # this attempt; the repair command separately checks DB readback.
        data.pop('content_audit', None)
        verified = capture_is_verified(api_meta, sections)
        data.update(capture_version=CAPTURE_VERSION, content_capture_version=CAPTURE_VERSION,
                    capture_source=('classic-ui+review-api' if source_kind == 'classic'
                                    else 'odata+review-api'), capture_verified=verified,
                    content_source_updated_at=review.get('UpdatedDate'))
        data['api_meta'] = api_meta
        data['sections'] = sections
    return data


def capture_is_verified(api_meta, sections):
    """A core manifest is not proof that every UI/previously saved section exists."""
    expected_count = len(api_meta.get('sections', [])) + len(api_meta.get('supplemental_sections', []))
    return (api_meta.get('capture_verified') is True
            and expected_count == len(sections or [])
            and all(s.get('capture_state') in VERIFIED_SECTION_STATES for s in sections or []))


def _visible_schema_text(value):
    """Normalize only documented HTML rendering and whitespace, never wording."""
    class TextParser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []

        def handle_data(self, value):
            self.parts.append(value)

        def handle_starttag(self, tag, attrs):
            if tag in {'br', 'p', 'div', 'li', 'ol', 'ul', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}:
                self.parts.append(' ')

        def handle_endtag(self, tag):
            self.handle_starttag(tag, [])

    if not isinstance(value, str):
        return None
    parser = TextParser()
    parser.feed(value)
    parser.close()
    return ' '.join(''.join(parser.parts).split())


def _is_schema_only_duplicate(previous, current):
    """Prove a legacy row contains only already-retained, unanswered schema.

    Browser captures sometimes stored question titles/descriptions as a second
    row beside the API capture. Every field and every visible character must
    be accounted for; actual answers or extra text rule out this exception.
    """
    if (current.get('capture_state') not in VERIFIED_SECTION_STATES
            or normalized(previous['section_name']) != normalized(current['section_name'])
            or previous.get('tables')):
        return False
    payload = current.get('source_payload')
    if not isinstance(payload, dict):
        return False
    if any(not _empty(previous.get(key)) and previous[key] != payload
           for key in ('source_payload', 'previous_source_payload')):
        return False
    declared, old_fields = payload.get('fields'), previous.get('fields')
    if (not isinstance(declared, list) or not declared or not isinstance(old_fields, list)
            or len(declared) != len(old_fields)):
        return False
    text_parts = []
    for old, source in zip(old_fields, declared):
        if not isinstance(old, dict) or not isinstance(source, dict):
            return False
        if any(not _empty(value) for key, value in old.items()
               if key not in ('label', 'description', 'value')):
            return False
        name = source.get('name')
        if not isinstance(name, str) or not name:
            return False
        if not _empty(old.get('value')) or not _empty(payload.get(name)):
            return False
        title = source.get('title') or name
        description = source.get('description') or ''
        if (_visible_schema_text(old.get('label', '')) != _visible_schema_text(title)
                or _visible_schema_text(old.get('description') or '') != _visible_schema_text(description)):
            return False
        text_parts.extend([title, description])
    expected = _visible_schema_text(' '.join(text_parts))
    return bool(expected) and _visible_schema_text(previous.get('raw_text') or '') == expected


def _is_plan_reviews_table_duplicate(previous, current):
    """Prove the legacy first-page schedule is contained in the full API list."""
    if (current.get('source_action') != 'PlanReviewsContent'
            or current.get('capture_state') not in VERIFIED_SECTION_STATES
            or normalized(previous['section_name']) != 'reviews schedule'
            or previous.get('fields')):
        return False
    payload = current.get('source_payload')
    if (not isinstance(payload, list) or len(payload) < 5
            or any(not isinstance(row, dict) for row in payload)):
        return False
    if any(not _empty(previous.get(key)) and previous[key] != payload
           for key in ('source_payload', 'previous_source_payload')):
        return False
    tables = previous.get('tables')
    if not isinstance(tables, list) or len(tables) != 2:
        return False
    for table in tables:
        if (not isinstance(table, dict)
                or any(not _empty(value) for key, value in table.items() if key != 'rows')):
            return False
    header = ['Review Name / Review Type', 'Planned Date', 'Actual Date']
    if tables[0].get('rows') != [header]:
        return False

    def display_date(value):
        if value is None or value == '':
            return ''
        if not isinstance(value, str):
            raise ValueError('Invalid source schedule date')
        return datetime.fromisoformat(value.replace('Z', '+00:00')).strftime('%d/%m/%Y')

    expected_rows = []
    try:
        for row in payload[:5]:
            if not isinstance(row.get('name'), str) or not isinstance(row.get('reviewType'), str):
                return False
            expected_rows.append([
                _visible_schema_text(row['name'] + ' ' + row['reviewType']),
                display_date(row.get('plannedDate')), display_date(row.get('actualDate'))])
    except (TypeError, ValueError):
        return False
    old_rows = tables[1].get('rows')
    if (not isinstance(old_rows, list) or len(old_rows) != 5
            or any(not isinstance(row, list) or len(row) != 3
                   or any(not isinstance(cell, str) for cell in row) for row in old_rows)):
        return False
    old_display_rows = [[_visible_schema_text(cell) for cell in row] for row in old_rows]
    for old_row, current_row in zip(old_display_rows, expected_rows):
        for column, (old_cell, current_cell) in enumerate(zip(old_row, current_row)):
            if old_cell != current_cell and not (column == 2 and old_cell == '' and current_cell):
                return False
    count = len(payload)
    pages = ' '.join(str(page) for page in range(1, (count + 4) // 5 + 1))
    pager = f'{pages} 5 items per page 1 - 5 of {count} items'
    # A newly recorded actual date may fill a previously blank cell. Validate
    # the old text against the fully accounted-for old table, not the new date.
    all_cells = header + [cell for row in old_display_rows for cell in row]
    expected_text = _visible_schema_text(' '.join(all_cells + [pager]))
    return _visible_schema_text(previous.get('raw_text') or '') == expected_text


def _is_learning_progress_empty_placeholder(previous, current):
    """Recognize Aptem's exact empty-state literal, with its source conditions."""
    payload = current.get('source_payload')
    return (
        current.get('source_action') == 'LearningProgressContent'
        and current.get('capture_state') in VERIFIED_SECTION_STATES
        and normalized(previous['section_name']) == normalized(current['section_name'])
        and not previous.get('fields') and not previous.get('tables')
        and previous.get('raw_text') == 'No information entered.'
        and isinstance(payload, dict) and payload.get('progress') == []
        and payload.get('hasPrevProgress') is False
        and all(_empty(previous.get(key)) or previous[key] == payload
                for key in ('source_payload', 'previous_source_payload'))
    )


def _is_skill_radar_ui_duplicate(previous, current):
    """Prove the exact, collapsed-history UI projection is in the raw source.

    This intentionally supports only the proven tenant role labels and layout.
    Unknown controls, role labels, dates or extra text preserve the old row.
    """
    if (current.get('source_action') != 'SkillRadarContent'
            or current.get('capture_state') not in VERIFIED_SECTION_STATES
            or normalized(previous['section_name']) != 'skills radar'
            or previous.get('tables')):
        return False
    expected_control = {'type': 'checkbox', 'label': 'Show History', 'value': 'on', 'checked': False}
    old_fields = previous.get('fields')
    if (old_fields != [expected_control] or old_fields[0].get('checked') is not False):
        return False
    payload = current.get('source_payload')
    if not isinstance(payload, dict):
        return False
    if any(not _empty(previous.get(key)) and previous[key] != payload
           for key in ('source_payload', 'previous_source_payload')):
        return False
    raw = previous.get('raw_text')
    if not isinstance(raw, str):
        return False
    first_line = next((line for line in raw.splitlines() if line.strip()), None)
    if first_line is None:
        return False
    # The tenant terminology endpoint overrides Tutor -> Skills Coach; its
    # absent Employer override retains the UI's literal Employer label.
    roles = {'Admin': ('bar-a', 'Skills Coach assessment'),
             'User': ('bar-b', 'Learner assessment'),
             'Employer': ('bar-i', 'Employer assessment')}

    def stamp(value):
        if value is None:
            return datetime(1970, 1, 1, tzinfo=timezone.utc)
        if not isinstance(value, str):
            raise ValueError('Unproven Skill Radar date representation')
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    try:
        groups = payload['competency_progress']
        if not isinstance(groups, list):
            return False
        matches = [group for group in groups
                   if _visible_schema_text(group['competency']['name']) == _visible_schema_text(first_line)]
        if len(matches) != 1:
            return False
        selected = matches[0]
        characteristics = selected['characteristics']
        if not isinstance(characteristics, list):
            return False
        types = {assessment['assessmentType'] for characteristic in characteristics
                 for assessment in characteristic['assessments']}
        if not types <= roles.keys():
            return False
        legend = [label for _, label in sorted({roles[role] for role in types})]
        lines = [selected['competency']['name'], 'Goal', *legend, 'Show History']
        for ordinal, characteristic in enumerate(characteristics, 1):
            actions, levels, assessments = (characteristic[key] for key in ('actions', 'levels', 'assessments'))
            if any(not isinstance(items, list) for items in (actions, levels, assessments)):
                return False
            latest = max((stamp(action['dateToComplete']) for action in actions), default=None)
            goals = [action['goalCharacteristicsLevel'] for action in actions
                     if stamp(action['dateToComplete']) == latest and action['goalCharacteristicsLevel']]
            goal = max((item['level'] for item in goals), default=None)
            ordered = sorted(assessments,
                             key=lambda row: (row['assessmentType'] == 'Admin', stamp(row['completedDate'])),
                             reverse=True)
            achieved = next((level['level'] for level in levels
                             if ordered and level['characteristicsLevelId'] == ordered[0]['characteristicsLevelId']), None)
            status = ('No Goal Set' if not goal else
                      'Achieved' if assessments and achieved is not None and achieved >= goal else
                      'Working Toward')
            lines.extend([str(ordinal), characteristic['name'], status])
            if assessments:
                lines.extend(['0'] + [str(level['level']) for level in levels])
        return _visible_schema_text(raw) == _visible_schema_text(' '.join(lines))
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return False


def _retained_payloads_compatible(previous, current):
    """Equal rendered text alone cannot prove that retained JSON is redundant."""
    payload = current.get('source_payload')
    return all(_empty(previous.get(key)) or previous[key] == payload
               for key in ('source_payload', 'previous_source_payload'))


def _proven_rag_alias(previous, current):
    return (current.get('source_action') == 'RagContent'
            and current.get('capture_state') in VERIFIED_SECTION_STATES
            and normalized(previous['section_name']) in ('rag', 'rag status')
            and previous.get('source_action') in (None, '', 'RagContent')
            and _retained_payloads_compatible(previous, current))


def _section_actions_compatible(previous, current):
    return previous.get('source_action') in (None, '', current.get('source_action'))


def _merge_preserved_sections(sections, previous):
    """Preserve prior content for failed reads, and any unmatched prior rows.

    Failed reads and a base manifest omission are never proof of deletion.
    The UI can add real sections independently of its base manifest. Missing
    nonempty rows remain explicit, unverified historical captures.
    """
    remaining = list(copy.deepcopy(previous))
    merged = []
    for section in sections:
        captured = copy.deepcopy(section)
        verified_rag = (captured.get('source_action') == 'RagContent'
                        and captured.get('capture_state') in VERIFIED_SECTION_STATES)
        matches = [(i, old) for i, old in enumerate(remaining)
                   if ((normalized(old['section_name']) == normalized(captured['section_name'])
                        and _section_actions_compatible(old, captured)
                        and (old.get('capture_state') != 'preserved_unverified'
                             or _retained_payloads_compatible(old, captured)))
                       or _proven_rag_alias(old, captured))]
        chosen = next(((i, old) for i, old in matches
                       if old.get('source_action') == captured.get('source_action')), None)
        if chosen is None and len(matches) == 1:
            chosen = matches[0]
        if chosen is None and verified_rag and matches:
            chosen = matches[0]
        if chosen is not None:
            index, prior = chosen
            remaining.pop(index)
            if captured.get('capture_state') not in VERIFIED_SECTION_STATES:
                if prior.get('fields') or prior.get('tables') or prior.get('raw_text'):
                    for key in ('fields', 'tables', 'raw_text'):
                        captured[key] = prior.get(key, '' if key == 'raw_text' else [])
                    captured['preserved_existing'] = True
                    if (prior.get('capture_state') not in VERIFIED_SECTION_STATES
                            and prior.get('previous_source_payload') is not None):
                        captured['previous_source_payload'] = prior['previous_source_payload']
                    elif prior.get('source_payload') is not None:
                        captured['previous_source_payload'] = prior['source_payload']
                    elif prior.get('previous_source_payload') is not None:
                        captured['previous_source_payload'] = prior['previous_source_payload']
        if verified_rag:
            # Aptem's source JS proves RAG Status is the display label of its
            # RAG panel. Legacy browser+API captures can contain both labels.
            remaining = [old for old in remaining
                         if not _proven_rag_alias(old, captured)]
        merged.append(captured)
    for prior in remaining:
        if not (prior.get('fields') or prior.get('tables') or prior.get('raw_text')):
            continue
        duplicate = any(
            normalized(prior['section_name']) == normalized(current['section_name'])
            and all(prior.get(key, '' if key == 'raw_text' else []) ==
                    current.get(key, '' if key == 'raw_text' else [])
                    for key in ('fields', 'tables', 'raw_text'))
            and _section_actions_compatible(prior, current)
            and _retained_payloads_compatible(prior, current)
            for current in merged)
        if not duplicate:
            duplicate = any(_section_actions_compatible(prior, current)
                            and _is_schema_only_duplicate(prior, current) for current in merged)
        if not duplicate:
            duplicate = any(_section_actions_compatible(prior, current)
                            and _is_plan_reviews_table_duplicate(prior, current) for current in merged)
        if not duplicate:
            duplicate = any(_section_actions_compatible(prior, current)
                            and _is_learning_progress_empty_placeholder(prior, current) for current in merged)
        if not duplicate:
            duplicate = any(_section_actions_compatible(prior, current)
                            and _is_skill_radar_ui_duplicate(prior, current) for current in merged)
        if duplicate:
            continue
        prior.update(capture_state='preserved_unverified', preserved_existing=True,
                     source_kind='historical_preserved',
                     capture_error='Existing nonempty section is absent from the captured source manifest')
        merged.append(prior)
    return merged


def save_live_review(review, learner_id, api_meta=None, sections=None, *, source_kind='odata'):
    """Atomic upsert; unavailable sections retain prior content and stay partial."""
    review_id = str(review['Id'])
    if source_kind not in ('odata', 'classic'):
        raise ReviewSyncError('Unknown review source kind')
    if (api_meta is None) != (sections is None):
        raise ReviewSyncError('Review content requires both manifest and section captures')
    if api_meta is not None:
        _validate_identity(api_meta, review_id, review['LearnerId'])
    if source_kind == 'classic':
        source_url = f'{settings.APTEM_BASE_URL.rstrip("/")}/MWS.Review/Review/Get/{review_id}'
    else:
        source_url = (f'{settings.APTEM_BASE_URL.rstrip("/")}/pwa/learners/'
                      f'{review["LearnerId"]}/reviews/review-content/{review_id}')
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute('''SELECT id,learner_id,review_data FROM "Learner".reviews
            WHERE aptem_review_id=%s FOR UPDATE''', [review_id])
        existing = cursor.fetchone()
        if existing and existing[1] != learner_id:
            raise ReviewSyncError('Stored review belongs to a different canonical learner')
        old_data = _decoded_json(existing[2], {}) if existing else {}
        if api_meta is not None and existing:
            cursor.execute('''SELECT section_name,fields_json,tables_json,raw_text
                FROM "Learner".review_sections WHERE review_id=%s
                ORDER BY section_order,id''', [existing[0]])
            previous_json = list(old_data.get('sections') or [])
            previous = []
            for name, fields, tables, text in cursor.fetchall():
                meta_index = next((i for i, item in enumerate(previous_json)
                                   if item.get('section_name') == name), None)
                meta = previous_json.pop(meta_index) if meta_index is not None else {}
                previous.append({**meta, 'section_name': name,
                                 'fields': _decoded_json(fields, []),
                                 'tables': _decoded_json(tables, []), 'raw_text': text or ''})
            sections = _merge_preserved_sections(sections, previous)
        data = _payload(review, api_meta, sections, old_data, source_kind=source_kind)
        raw_text = None
        extraction = 'partial'
        error = 'Aptem metadata only; review content has not been verified.'
        if sections is not None:
            raw_text = '\n\n'.join(s['section_name']+'\n'+s['raw_text'] for s in sections).strip()
            if data.get('capture_verified'):
                extraction, error = 'success', None
            else:
                failures = [s.get('capture_error') for s in sections if s.get('capture_error')]
                if api_meta.get('manifest_reconstructed'):
                    failures.append('Target section manifest is unavailable; template membership is unverified')
                error = '; '.join(dict.fromkeys(failures)) or 'Review capture has not been verified'
        elif existing:
            cursor.execute('SELECT raw_text,extraction_status,last_error FROM "Learner".reviews WHERE id=%s', [existing[0]])
            raw_text, extraction, error = cursor.fetchone()
        encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        values = [(review.get('Name') or '').strip(), (review.get('Type') or '').strip(),
                  review.get('OwnerName'), review.get('PlannedDate'), review.get('CompletedDate'),
                  display_status(review.get('Status')), source_url, encoded, raw_text, digest,
                  extraction, error]
        if existing:
            pk = existing[0]
            cursor.execute('''UPDATE "Learner".reviews SET review_name=%s,review_type=%s,
                reviewer_name=%s,planned_scheduled_date=%s,completed_date=%s,status=%s,
                source_url=%s,review_data=%s::jsonb,raw_text=%s,content_hash=%s,
                extraction_status=%s,last_error=%s,last_seen_at=now(),updated_at=now(),
                last_extracted_at=CASE WHEN %s THEN now() ELSE last_extracted_at END
                WHERE id=%s''', values + [sections is not None, pk])
        else:
            cursor.execute('''INSERT INTO "Learner".reviews
                (learner_id,aptem_review_id,review_name,review_type,reviewer_name,
                 planned_scheduled_date,completed_date,status,source_url,review_data,raw_text,
                 content_hash,extraction_status,last_error,learner_name,learner_email,
                 first_seen_at,last_seen_at,last_extracted_at,created_at,updated_at)
                SELECT l.id,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,
                       l.full_name,l.email,now(),now(),CASE WHEN %s THEN now() END,now(),now()
                FROM "Learner".learners l WHERE l.id=%s RETURNING id''',
                [review_id] + values + [sections is not None, learner_id])
            row = cursor.fetchone()
            if not row:
                raise ReviewSyncError('Canonical learner disappeared during review insert')
            pk = row[0]
        if sections is not None:
            cursor.execute('DELETE FROM "Learner".review_sections WHERE review_id=%s', [pk])
            section_values = []
            for order, section in enumerate(sections, 1):
                section_values.extend([
                    pk, section['section_name'], order,
                    json.dumps(section['fields'], ensure_ascii=False),
                    json.dumps(section['tables'], ensure_ascii=False), section['raw_text']])
            if sections:
                placeholders = ','.join(
                    '(%s,%s,%s,%s::jsonb,%s::jsonb,%s,now(),now())' for _ in sections)
                cursor.execute('''INSERT INTO "Learner".review_sections
                    (review_id,section_name,section_order,fields_json,tables_json,raw_text,
                     created_at,updated_at) VALUES ''' + placeholders, section_values)
        cursor.execute('''UPDATE "Learner".reviews r SET learner_name=l.full_name,
            learner_email=l.email FROM "Learner".learners l
            WHERE r.id=%s AND l.id=r.learner_id''', [pk])
    return pk
