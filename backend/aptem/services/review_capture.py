"""Capture the expanded, live-verified review and archive attachment links."""
import hashlib
from urllib.parse import quote, urlsplit, urljoin
import requests
from django.conf import settings
from .review_probe import check_session


def capture_review(page, learner_id, review_id, metadata):
    from playwright.sync_api import expect

    check_session(page)
    page.wait_for_load_state('networkidle', timeout=60000)
    expect(page.locator('accordion-group').first).to_be_visible()
    expect(page.locator('[aria-busy="true"]')).to_have_count(0)
    sections = []
    index = 0
    while index < page.locator('accordion-group').count():
        group = page.locator('accordion-group').nth(index)
        section_name = group.locator('div[role="button"][aria-expanded]').inner_text().strip()
        toggle = group.locator('div[role="button"][aria-expanded]')
        if toggle.get_attribute('aria-expanded') == 'false':
            toggle.click()
        expect(toggle).to_have_attribute('aria-expanded', 'true')
        page.wait_for_load_state('networkidle', timeout=60000)
        expect(page.locator('[aria-busy="true"]')).to_have_count(0)
        panel = group.get_by_role('tabpanel')
        expect(panel).to_be_visible()
        # Allow the lazy section request and Angular render to settle. A populated
        # learner always has identity content; never accept its loading shell.
        if section_name == 'Learner Information':
            expect(panel).to_contain_text('Name', timeout=30000)
        previous_text = panel.inner_text()
        for settle_attempt in range(3):
            page.wait_for_timeout(700)
            page.wait_for_load_state('networkidle', timeout=60000)
            current_text = panel.inner_text()
            if current_text == previous_text:
                break
            previous_text = current_text
        else:
            raise ValueError('Section content did not settle; refusing incomplete capture')
        data = panel.evaluate('''panel => {
            const text = e => (e.innerText || '').trim();
            const fields = [];
            // Verified Aptem read-only custom fields: name, description, then value nodes.
            for (const field of panel.querySelectorAll('dynamic-plain-fields field')) {
                const label = field.querySelector(':scope > name');
                if (!label) continue;
                const description = field.querySelector(':scope > description');
                const values = Array.from(field.children).filter(e =>
                    !['NAME','DESCRIPTION'].includes(e.tagName)).map(text);
                fields.push({label:text(label), description:description ? text(description) : '',
                    value:values.join('\\n').trim()});
            }
            // Verified learner-information layout: heading container followed by value container.
            for (const h of panel.querySelectorAll('learner-information h2')) {
                if (!text(h).endsWith(':')) continue;
                const value = h.parentElement.nextElementSibling;
                if (value) fields.push({label:text(h), value:text(value)});
            }
            for (const input of panel.querySelectorAll('input,textarea,select')) {
                if (input.type === 'hidden' || input.type === 'password') continue;
                fields.push({label: Array.from(input.labels || []).map(text).join(' ') ||
                    input.getAttribute('aria-label') || input.name || input.id || '',
                    value: input.value, type:input.type,
                    checked: ['checkbox','radio'].includes(input.type) ? input.checked : null});
            }
            const tables = Array.from(panel.querySelectorAll('table')).map(table => ({
                rows:Array.from(table.rows).map(row => Array.from(row.cells).map(text))
            }));
            const links = Array.from(panel.querySelectorAll('a')).map(a => ({
                text: text(a), href: a.href || a.getAttribute('href') || '',
                download: a.getAttribute('download') || null,
                aria_label: a.getAttribute('aria-label') || null,
                title: a.getAttribute('title') || null
            }));
            return {fields, tables, links, raw_text:text(panel), raw_html:panel.innerHTML};
        }''')
        data['section_name'] = toggle.inner_text().strip()
        data['load_verified'] = True
        data['empty_after_load'] = not bool(data['raw_text'].strip())
        sections.append(data)
        index += 1
    # The authoritative live status check happens on the learner dashboard before
    # this function is called. Review detail pages do not consistently repeat the
    # programme-status field, so absence of that optional field must not invalidate
    # an otherwise successfully live-verified learner.
    status = 'Active'
    names = [s.strip() for s in metadata.get('Review Name / Review Type', '').splitlines() if s.strip()]
    if not names or metadata.get('Status') not in ('Completed', 'In Progress'):
        raise ValueError('Review metadata must identify a Completed or In Progress review')
    container_url = getattr(settings, 'AZURE_REVIEWS_CONTAINER_URL', '').strip()
    if container_url:
        parts = urlsplit(container_url)
        if not parts.query or 'sp=' not in parts.query or 'w' not in parts.query.split('sp=',1)[1].split('&',1)[0]:
            raise ValueError('Azure reviews SAS does not grant write permission')
        uploaded = []
        for section in sections:
            for link in section.get('links', []):
                href = urljoin(page.url, link.get('href') or '')
                if not href.startswith('https://'):
                    continue
                response = page.request.get(href)
                if not response.ok:
                    raise ValueError(f'Attachment download failed: HTTP {response.status}')
                body = response.body()
                digest = hashlib.sha256(href.encode()).hexdigest()[:20]
                filename = f'learner-{learner_id}/review-{review_id}/{digest}'
                blob_url = container_url.split('?', 1)[0].rstrip('/') + '/' + quote(filename, safe='/') + '?' + parts.query
                upload = requests.put(blob_url, data=body, headers={
                    'x-ms-blob-type': 'BlockBlob',
                    'Content-Type': response.headers.get('content-type', 'application/octet-stream'),
                }, timeout=60)
                if upload.status_code not in (201, 202):
                    raise ValueError(f'Azure upload failed: HTTP {upload.status_code}')
                link['azure_url'] = blob_url
                link['azure_blob_name'] = filename
                uploaded.append(blob_url)

    return {
        'aptem_learner_id': learner_id, 'aptem_review_id': review_id,
        'programme_status': status, 'status': metadata['Status'],
        'review_name': names[0], 'review_type': names[-1],
        'reviewer_name': metadata.get('Reviewer'), 'source_url': page.url,
        'source_metadata': metadata, 'sections': sections,
        'capture_version': 3,
        'raw_text': '\n\n'.join(s['section_name']+'\n'+s['raw_text'] for s in sections),
    }
