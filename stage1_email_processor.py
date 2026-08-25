import os.path
import base64
import re
import html
from datetime import datetime

from app.google_runtime import build_oauth_services

# API Scopes for Gmail (Read/Modify) and Google Sheets (Read/Write)
SCOPES = [
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/spreadsheets'
]

# The ID of the bid_board_emails Google Sheet
def env_value(name, default):
    return os.getenv(name, default).strip()


SPREADSHEET_ID = env_value('EMAILS_SHEET_ID', '1mhCWXwSUtV-AbBxLmEBS-jezkeujVPMYY9RD5ENlPFU')
PREFERRED_SHEET_NAME = env_value('EMAILS_TAB_NAME', 'bid_board_emails')
COLUMNS_RANGE = 'A:E'

def authenticate_google_services():
    """Authenticates and returns the Gmail and Sheets service objects."""
    # Gmail access must use OAuth user credentials; the same token can also
    # write to Sheets because this workflow consumes Gmail and appends rows.
    return build_oauth_services(
        ("gmail", "sheets"),
        SCOPES,
        token_filename="stage1_token.json",
        use_file_env=False,
    )

def get_sheet_range(sheets_service):
    """Returns a valid A:E range for the preferred sheet or the first tab."""
    spreadsheet = sheets_service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    sheets = spreadsheet.get('sheets', [])

    if not sheets:
        raise ValueError("No sheets were found in the target spreadsheet.")

    available_sheet_names = [
        sheet.get('properties', {}).get('title', '')
        for sheet in sheets
    ]

    if PREFERRED_SHEET_NAME in available_sheet_names:
        sheet_name = PREFERRED_SHEET_NAME
    else:
        sheet_name = available_sheet_names[0]
        print(
            f"Sheet tab '{PREFERRED_SHEET_NAME}' was not found. "
            f"Using '{sheet_name}' instead."
        )

    # Quote the sheet name so names with spaces/special characters work.
    escaped_sheet_name = sheet_name.replace("'", "''")
    return f"'{escaped_sheet_name}'!{COLUMNS_RANGE}"

def get_unread_bid_emails(gmail_service):
    """Fetches unread emails from the specific sender from the last 24 hours (temporarily changed to 5 days)."""
    query = "from:team@buildingconnected.com is:unread newer_than:5d"  #team@buildingconnected.com  #info@comptonsales.com
    results = gmail_service.users().messages().list(userId='me', q=query).execute()
    return results.get('messages', [])

def decode_gmail_body(data):
    """Decodes a Gmail API body payload."""
    return base64.urlsafe_b64decode(data).decode('utf-8', errors='replace')

def strip_html(html_content):
    """Converts simple HTML email content into readable plain text."""
    text = re.sub(r'(?i)<br\s*/?>', '\n', html_content)
    text = re.sub(r'(?i)</p>|</div>|</tr>|</li>|</h\d>', '\n', text)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n', text)
    return text.strip()

def extract_text_from_payload(payload):
    """Recursively extracts the best available text body from a Gmail payload."""
    mime_type = payload.get('mimeType', '')
    body = payload.get('body', {})
    data = body.get('data')

    if mime_type == 'text/plain' and data:
        return decode_gmail_body(data)

    parts = payload.get('parts', [])
    for part in parts:
        text = extract_text_from_payload(part)
        if text:
            return text

    if mime_type == 'text/html' and data:
        return strip_html(decode_gmail_body(data))

    if data:
        decoded = decode_gmail_body(data)
        return strip_html(decoded) if '<' in decoded and '>' in decoded else decoded

    return ""

def get_email_content(gmail_service, msg_id):
    """Extracts the subject line and decodes the email body."""
    msg = gmail_service.users().messages().get(userId='me', id=msg_id, format='full').execute()
    payload = msg.get('payload', {})
    headers = payload.get('headers', [])
    
    subject = "No Subject"
    for header in headers:
        if header.get('name') == 'Subject':
            subject = header.get('value')
            break

    body = extract_text_from_payload(payload)

    return subject, body

PROJECT_STOP_MARKERS = [
    r'View this RFP',
    r'View the RFP',
    r'Already know if',
    r'Project Details',
    r'Client Details',
    r'Location:',
    r'Bid Due:',
    r'Manage all your Bids',
    r'Start now',
    r'Bidding',
    r'Not Bidding',
    r'Not Sure',
    r'\[image:',
    r'<https?://',
]

BLOCKED_PROJECT_LABELS = {
    'attachments',
    'bid due',
    'client details',
    'date',
    'from',
    'lead',
    'location',
    'project details',
    'reply-to',
    'subject',
    'to',
    'to all',
}


def clean_text_fragment(value):
    """Removes forwarding/Markdown decoration without changing title punctuation."""
    value = html.unescape(value or "")
    value = re.sub(r"^[\s\"`*_•\-:]+", "", value).strip()
    value = re.sub(r"[\s\"`*_•\-:]+$", "", value).strip()
    return re.sub(r"\s+", " ", value).strip()


def split_project_scope(value):
    """Splits BuildingConnected's ``Project*: Scope`` visual convention."""
    value = re.sub(
        rf"\s*(?:{'|'.join(PROJECT_STOP_MARKERS)}).*$",
        "",
        value or "",
        flags=re.IGNORECASE,
    ).strip()

    # Plain-text forwarded messages retain Markdown emphasis around the project:
    # "*Gustavo's*: Roofing". Prefer that explicit boundary. For HTML-derived
    # text, the same content may be rendered as "Gustavo's: Roofing".
    match = re.match(r"^(?P<project>.+?)\*+\s*:\s*(?P<scope>.+)$", value)
    if not match and ':' in value:
        project, scope = value.rsplit(':', 1)
        match_values = (project, scope)
    elif match:
        match_values = (match.group("project"), match.group("scope"))
    else:
        match_values = (value, "")

    project = clean_text_fragment(match_values[0])
    scope = clean_text_fragment(match_values[1])
    return project, scope


def clean_project_name(project_name):
    """Returns only the normalized project portion of a candidate value."""
    project, _ = split_project_scope(project_name)
    return project


def extract_subject_project(subject):
    """Extracts a complete BuildingConnected project name from the subject."""
    if not subject or re.search(r"\.\.\.|…", subject):
        return "Unknown"

    normalized = re.sub(r"^(?:(?:fwd?|re)\s*:\s*)+", "", subject, flags=re.IGNORECASE)
    match = re.search(
        r"\bbid invite:\s*(?P<project>.+?)\s+project\s*$",
        normalized,
        re.IGNORECASE,
    )
    if not match:
        return "Unknown"

    candidate = clean_text_fragment(match.group("project"))
    return candidate if is_valid_project_name(candidate) else "Unknown"


def extract_invitation_project(body):
    """Extracts project and scope from the title following the invite phrase."""
    normalized_body = re.sub(r"\s+", " ", body or "").strip()
    match = re.search(
        r"has invited you\s+to bid on\s+(.+?)"
        r"(?=\s+View (?:this|the) RFP\b|\s+Already know if\b|"
        r"\s+Project Details\b|\s+Client Details\b|$)",
        normalized_body,
        re.IGNORECASE,
    )
    if not match:
        return "Unknown", ""

    project, scope = split_project_scope(match.group(1))
    if not is_valid_project_name(project):
        return "Unknown", ""
    return project, scope


def normalize_project_key(project_name):
    """Creates a stable key so similar project names collapse to one value."""
    if not project_name:
        return "unknown"

    key = clean_project_name(project_name).lower()
    key = key.replace("&", "and")
    key = re.sub(r"[^a-z0-9]+", " ", key)
    key = re.sub(r"\s+", " ", key).strip()
    return key


def extract_colon_project_name(lines):
    """Last-resort parser for a likely 'Project Name: Scope' line."""
    skip_prefixes = (
        'location:',
        'bid due:',
        'project details',
        'client details',
        'already know if',
        'manage all your bids',
        'view this rfp',
        'view the rfp',
        'to all:',
        'attachments:',
        'subject:',
        'date:',
        'from:',
        'to:',
        'reply-to:',
    )

    skip_line_patterns = [
        r'https?://',
        r'www\.',
        r'\.pdf\b',
        r'\.docx?\b',
        r'\.xlsx?\b',
        r'\.zip\b',
        r'\breply to\b',
        r'\bsend bid\b',
        r'\bview (this )?rfp\b',
        r'\bmanage all your bids\b',
        r'\battached is\b',
        r'\battachments?\b',
    ]

    blocked_left_patterns = [
        r'\brequested\b',
        r'\binvited\b',
        r'\bmessage\b',
        r'\bbudget\b',
        r'\bpricing\b',
        r'\bcontact\b',
        r'\bemail\b',
        r'\breply\b',
        r'\battachment\b',
        r'\baddendum\b',
    ]

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Formatting such as "*Bid Due: *August 14" must not bypass labels.
        normalized_line = re.sub(r"[*_`]+", "", line).strip()
        lower = normalized_line.lower()

        if any(lower.startswith(prefix) for prefix in skip_prefixes):
            continue

        if any(re.search(pattern, lower, re.IGNORECASE) for pattern in skip_line_patterns):
            continue

        if ':' not in normalized_line:
            continue

        left, right = normalized_line.rsplit(':', 1)
        left = left.strip()
        right = right.strip()

        if not left or not right:
            continue

        # Ignore sentence-like intros, but allow legitimate one-word projects.
        left_word_count = len(left.split())
        if left_word_count > 20:
            continue

        if any(re.search(pattern, left, re.IGNORECASE) for pattern in blocked_left_patterns):
            continue

        candidate = clean_project_name(left)

        # Reject junky remnants
        if not candidate:
            continue
        if re.search(r'https?|www|\.pdf|\.doc|\.xls|\.zip', candidate, re.IGNORECASE):
            continue
        if not re.search(r'[A-Za-z0-9]', candidate):
            continue

        if is_valid_project_name(candidate):
            return candidate

    return "Unknown"


def is_valid_project_name(project_name):
    """Rejects obvious UI text accidentally captured as a project name."""
    if not project_name or project_name == "Unknown":
        return False

    normalized_label = re.sub(r"[*_`]+", "", project_name).strip(" :").lower()
    if normalized_label in BLOCKED_PROJECT_LABELS:
        return False

    invalid_patterns = [
        r'^this rfp\??',
        r'^view (?:this|the) rfp',
        r'^already know if',
        r'\blet .+ at\b',
        r'^bidding$',
        r'^not bidding$',
        r'^not sure$',
    ]

    return not any(re.search(pattern, project_name, re.IGNORECASE) for pattern in invalid_patterns)

# Identify national brands
SKIP_BRANDS = []
# SKIP_BRANDS = ["advance auto parts", "aldi", "autozone", "barnes & noble", "basss pro shops", "bath & body works", 
# "best western", "boa", "bojangles", "chick-fil-a", "circle k", "cvs", "dollar tree", "dutch bros", "fifth third bank", 
# "five guys", "floor & decor", "harbor freight", "holiday inn", "home 2 suites", "hyatt", "insomnia cookies", "kia", 
# "kroger", "lidl", "long john silvers", "members exchange", "officemax", "old navy", "o’reilly", "pilot", "prequal", 
# "publix", "sam’s club", "savers", "smuckers", "speedway", "sprouts", "staybridge suites", "ta travel center", 
# "total wine & more", "toyota", "tractor supply", "u-haul", "us bank", "usps", "valvoline", "victoria’s secret", 
# "visionworks", "wawa", "walmart", "whole foods", "wingstop", "winn-dixie", "wm", "xfinity", "zaxby"]

# Identify words indicating email is not of interest
SKIP_WORDS = ["are you", "bafo", "budget", "cad", "cd", "checking in", "compliance", "do not use", "drawing", 
"due", "ext", "final", "follow up", "follow-up", "forms", "friendly", "geotech", "help", "hvac", "instructions", 
"job", "landscaping", "last call", "lots", "message", "mock up", "nda", "no longer bidding", "notice", "notes", 
"overdue", "paint", "photos", "prebid", "prequal", "proposal", "qualification", "reminder", "reordered", 
"report", "response", "retail", "rfi", "rough", "schedule", "site", "tell me", "thank you", "twic", "unit prices", 
"update", "urgent", "visit", "walk through", "will you", "work"]

BID_LIST = ["bid invite", "invitation to bid", "itb", "plans", "request for proposal"]

def classify_email(subject, body):
    """Classifies the email based on the subject and body rules."""
    subject_lower = subject.lower().strip()
    body_lower = body.lower()

    # Rule to rule them all:
    # Any Bid Invite is kept unless it is specifically a reminder.
    if "bid invite" in subject_lower:
        if subject_lower.startswith("reminder: bid invite"):
            return "SKIP"
        return "New Project"

    # Existing skip conditions
    if any(phrase in subject_lower for phrase in SKIP_WORDS) or "site visit" in body_lower:
        return "SKIP"

    if any(brand in subject_lower for brand in SKIP_BRANDS):
        return "SKIP"

    if any(phrase in subject_lower for phrase in BID_LIST):
        return "New Project"
    elif "addendum" in subject_lower or "add" in subject_lower or "addendum" in body_lower:
        return "Addendum"
    elif "specifications" in subject_lower or "specs" in subject_lower or "specifications" in body_lower or "specs" in body_lower:
        return "Specifications"
    elif "revised" in subject_lower or "correction" in subject_lower or "revised" in body_lower:
        return "Revision"

    return "Other"

def extract_project_details(body, subject=""):
    """Extracts a validated project name and associated document filenames."""
    project_name = "Unknown"
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    body_project, _ = extract_invitation_project(body)
    subject_project = extract_subject_project(subject)

    if body_project != "Unknown":
        # The invitation body matches the canonical Bid Board name. Work scope
        # (for example Roofing or Caulking) is deliberately discarded because
        # multiple scope invites can point to the same project and files.
        project_name = body_project
    elif subject_project != "Unknown":
        # Subjects are only a fallback because forwarding clients can truncate
        # or otherwise alter them.
        project_name = subject_project

    # Legacy update/addendum emails may not use the invitation template.
    if project_name == "Unknown" and body_project == "Unknown" and subject_project == "Unknown":
        stop_patterns = [
            r'^addendum\b',
            r'^due date\b',
            r'^attachments?:\b',
            r'^good morning\b',
            r'^good afternoon\b',
            r'^good evening\b',
            r'^please\b',
            r'^reply to\b',
            r'^send bid\b',
            r'^view (this|the) rfp\b',
            r'^already know if\b',
            r'^manage all your bids\b',
            r'^project details\b',
            r'^client details\b',
            r'^location:',
            r'^bid due:',
        ]

        def looks_like_stop_line(text):
            normalized = re.sub(r"[*_`]+", "", text).strip()
            return any(re.search(pattern, normalized, re.IGNORECASE) for pattern in stop_patterns)

        for index, line in enumerate(lines):
            if re.search(r'message about\s*$', line, re.IGNORECASE):
                if index + 1 < len(lines):
                    candidate = clean_project_name(lines[index + 1])
                    if is_valid_project_name(candidate) and not looks_like_stop_line(candidate):
                        project_name = candidate
                        break

            inline_match = re.search(
                r'about\s+(.+?)(?=:\s|Addendum\b|Due Date\b|Attachments?:\b|'
                r'Good Morning\b|Good Afternoon\b|Good Evening\b|Please\b|'
                r'Reply to\b|Send Bid\b|$)',
                line,
                re.IGNORECASE,
            )
            if inline_match:
                candidate = clean_project_name(inline_match.group(1).strip())
                if is_valid_project_name(candidate) and not looks_like_stop_line(candidate):
                    project_name = candidate
                    break

        if project_name == "Unknown":
            normalized_body = re.sub(r'\s+', ' ', body).strip()
            project_patterns = [
                r'message about\s+(.+?)(?=\s+Addendum\b|\s+Due Date\b|'
                r'\s+Attachments?:\b|\s+Good Morning\b|\s+Good Afternoon\b|'
                r'\s+Good Evening\b|\s+Please\b|\s+Reply to\b|\s+Send Bid\b|'
                r'\s+View (?:this|the) RFP|\s+Project Details|\s*$)',
                r'about\s+(.+?)(?=:\s|\s+View (?:this|the) RFP|'
                r'\s+Project Details|\s+Addendum\b|\s+Due Date\b|'
                r'\s+Attachments?:\b|\s*$)',
            ]
            for pattern in project_patterns:
                match = re.search(pattern, normalized_body, re.IGNORECASE)
                if match:
                    candidate = clean_project_name(match.group(1).strip())
                    if is_valid_project_name(candidate) and not looks_like_stop_line(candidate):
                        project_name = candidate
                        break

        # Generic colon parsing is intentionally last because forwarded
        # messages contain many metadata labels using the same punctuation.
        if project_name == "Unknown":
            project_name = extract_colon_project_name(lines)

    # Extract Filenames
    file_pattern = r'([a-zA-Z0-9_\-\s\(\)]+\.(?:pdf|zip|docx|xlsx)|Addendum\s*#?\d+)'
    found_files = re.findall(file_pattern, body, re.IGNORECASE)

    document_links = "\n".join(list(set(found_files))) if found_files else ""
    return project_name, document_links

def mark_as_read(gmail_service, msg_id):
    """Removes the UNREAD label from the processed email."""
    gmail_service.users().messages().modify(
        userId='me', id=msg_id, body={'removeLabelIds': ['UNREAD']}
    ).execute()

def process_pipeline():
    """Main execution function coordinating fetching, parsing, and logging."""
    gmail_service, sheets_service = authenticate_google_services()
    sheet_range = get_sheet_range(sheets_service)
    messages = get_unread_bid_emails(gmail_service)
    
    if not messages:
        print("No new unread emails found.")
        return

    seen_projects = set()

    for msg in messages:
        msg_id = msg['id']
        try:
            subject, body = get_email_content(gmail_service, msg_id)
            category = classify_email(subject, body)
            
            # If rules indicate skipping, mark as read and do not log
            if category == "SKIP":
                mark_as_read(gmail_service, msg_id)
                continue
            
            project_name, docs = extract_project_details(body, subject)
            if not is_valid_project_name(clean_project_name(project_name)):
                category = "Other"
                project_name = "Unknown"
            project_key = normalize_project_key(project_name)

            if project_key in seen_projects:
                mark_as_read(gmail_service, msg_id)
                print(f"Skipping duplicate project: {project_name}")
                continue

            seen_projects.add(project_key)

            date_str = datetime.now().strftime("%m/%d/%Y")
            row_data = [date_str, subject, project_name, category, docs]
            
            # Append row to Google Sheet
            sheets_service.spreadsheets().values().append(
                spreadsheetId=SPREADSHEET_ID,
                range=sheet_range,
                valueInputOption="USER_ENTERED",
                body={"values": [row_data]}
            ).execute()
            
            # Only mark as read if the append is successful
            mark_as_read(gmail_service, msg_id)
            print(f"Successfully processed and logged: {project_name}")
            
        except Exception as e:
            # Error Handling: Log the error to the sheet, but don't mark as read
            error_row = [datetime.now().strftime("%m/%d/%Y"), subject if 'subject' in locals() else "Unknown", "Unknown", "Processing Error", str(e)]
            sheets_service.spreadsheets().values().append(
                spreadsheetId=SPREADSHEET_ID,
                range=sheet_range,
                valueInputOption="USER_ENTERED",
                body={"values": [error_row]}
            ).execute()
            print(f"Error processing email ID {msg_id}: {str(e)}")

if __name__ == '__main__':
    process_pipeline()