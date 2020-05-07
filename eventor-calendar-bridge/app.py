
# TODO I believe I can optimize imports, to reduce size of deployment zip file (eventor-calendar-bridge > .chalice > deployments)
# currently it is 9.2 MB - try to get it smaller, while still making things work
import logging
import google.oauth2
import requests
import untangle
import google_auth_oauthlib.flow
import googleapiclient.discovery
import datetime
from dateutil import tz, parser

import json
import boto3
from botocore.exceptions import ClientError

from chalice import NotFoundError, Response
from chalice import Chalice, Rate


def add_to_s3(bucket, key, body):
    app.log.debug('adding to s3 bucket: ' + bucket + ', key: ' + key)
    S3.put_object(Bucket=bucket, Key=key, Body=body)


def get_from_s3(bucket, key):
    app.log.debug('reading from s3 bucket: ' + bucket + ', key: ' + key)
    response = S3.get_object(Bucket=bucket, Key=key)
    return response['Body'].read()


def get_from_s3_safe(bucket, key, default):
    try:
        return get_from_s3(bucket, key)
    except ClientError as e:
        return default


app = Chalice(app_name='eventor-calendar-bridge')
app.debug = True
app.log.setLevel(logging.DEBUG)

S3 = boto3.client('s3', region_name='eu-west-1')
BUCKET = 'eventor-google-calendar'

# This OAuth 2.0 access scope allows for full read/write access to the
# authenticated user's account and requires requests to use an SSL connection.
# TODO At the moment, need both scopes, even though I only wanted the first. Seems the second is sticking in google responses
SCOPES = ['https://www.googleapis.com/auth/calendar.readonly', 'https://www.googleapis.com/auth/drive.metadata.readonly', 'https://www.googleapis.com/auth/calendar']
API_SERVICE_NAME = 'calendar'
API_VERSION = 'v3'

CONFIG = json.loads(get_from_s3_safe(BUCKET, 'calendar-config', '{}'))
EVENTOR_ORGS = json.loads(get_from_s3_safe(BUCKET, 'eventor-orgs', '{}'))

EVENTOR_BASE_URL = 'https://eventor.orientering.no/api'

EVENTOR_HEADERS = {
    'ApiKey': (CONFIG['EventorApiKey'])
}

BASE_URL = CONFIG['HostAddress'] + '/api'
TIME_ZONE_NAME = 'Europe/Paris'


def init():
    app.log.info('Managed to read configuration OK')
    if not EVENTOR_ORGS or len(EVENTOR_ORGS) < 10:
        app.log.info('Count of eventor organisations is suspiciously low: ' + str(len(EVENTOR_ORGS)) + ', download it')
        update_eventor_orgs_list()
    app.log.info('Count of eventor organisations: ' + str(len(EVENTOR_ORGS)))
    app.log.info('Initialization complete, waiting for something to do ...')


def build_eventor_api_url(relative_path):
    return EVENTOR_BASE_URL + relative_path


def get_from_eventor(path):
    app.log.debug('Fetching events from eventor: ' + path)
    response = requests.request('GET', build_eventor_api_url(path),
                                headers=EVENTOR_HEADERS)
    response.raise_for_status()
    return response.text


def int_list_to_comma_sep_string(int_list):
    return ','.join(map(str, int_list))


def get_datetime_iso(datetime_string):
    default_date = datetime.datetime.combine(datetime.datetime.now(), datetime.time(0, tzinfo=tz.gettz(TIME_ZONE_NAME)))
    dt = parser.parse(datetime_string, default=default_date)
    return dt.isoformat()


def get_events_from_eventor(organisation_ids):
    organisation_ids_comma_sep = int_list_to_comma_sep_string(organisation_ids)
    format_date = '{0:%Y-%m-%d}'
    now = datetime.datetime.now()
    future = get_future_date(now)
    xml_text = get_from_eventor('/events?fromDate=' + format_date.format(now) + '&toDate=' + format_date.format(future)
                                + '&organisationIds=' + organisation_ids_comma_sep)
    event_list = untangle.parse(xml_text)
    calendar_events = []
    try:
        for event in event_list.EventList.Event:
            # app.log.debug('---------------')
            # app.log.debug('All Event: ' + str(event))
            app.log.debug('Processing event with event-id: ' + event.EventId.cdata)
            # app.log.debug('Event: ' + event.Name.cdata)
            # app.log.debug('Start dato: ' + event.StartDate.Date.cdata)
            # app.log.debug('Start klokke: ' + event.StartDate.Clock.cdata)
            # app.log.debug('Stopp dato: ' + event.FinishDate.Date.cdata)
            # app.log.debug('Stopp klokke: ' + event.FinishDate.Clock.cdata)
            event_id = event.EventId.cdata
            link = 'http://eventor.orientering.no/Events/Show/' + event_id
            location = ''
            try:
                location = event.EventRace.EventCenterPosition['y'] + ', ' + event.EventRace.EventCenterPosition['x']
            except:
                app.log.debug('EventCenterPosition is unavailable for event  ' + event_id)

            org_name_list = []
            for organiserId in event.Organiser.OrganisationId:
                # app.log.debug('organiser: ' + str(organiserId.cdata))
                org_name_list.append(EVENTOR_ORGS.get(organiserId.cdata, '?'))

            start_time = get_datetime_iso(event.StartDate.Date.cdata + ' ' + event.StartDate.Clock.cdata)
            end_time = get_datetime_iso(event.FinishDate.Date.cdata + ' ' + event.FinishDate.Clock.cdata)
            organisers_name = str.join(', ', org_name_list)
            calendar_event = {
                'summary': event.Name.cdata + ' (' + organisers_name + ')',
                'location': location,
                'description': 'Se flere detaljer i Eventor: ' + link,
                'start': {
                    'dateTime': start_time,
                    'timeZone': TIME_ZONE_NAME,
                },
                'end': {
                    'dateTime': end_time,
                    'timeZone': TIME_ZONE_NAME,
                },
                # Remove reminders for now
                # 'reminders': {
                #     'useDefault': False,
                #     'overrides': [
                #         {'method': 'popup', 'minutes': 24 * 60}
                #     ],
                # },
                'source': {
                    'title': event_id,
                    'url': link
                },
                'transparency': 'transparent',
                'visibility': 'public'
            }

            # Remove location-field if it is empty, in order to facilitate comparing these events to those retrieved from
            # google calendar later
            if location == '':
                del calendar_event['location']
            calendar_events.append(calendar_event)
    except:
        app.log.warn('Could not parse events for org ids: ' + organisation_ids_comma_sep)
        app.log.warn('Could not parse events for org ids, xml response from eventor: ' + xml_text)
    app.log.debug('Found ' + str(len(calendar_events)) + ' events in Eventor for these org ids: '
                  + organisation_ids_comma_sep)
    return calendar_events


def get_code_from_request():
    req_details = app.current_request.to_dict()
    code = req_details['query_params']['code']
    return code


# Automatically runs twice every day
@app.schedule(Rate(60 * 12, unit=Rate.MINUTES))
def periodic_task(event):
    return sync_eventor_with_google_calendar()


@app.route('/sync-google-eventor')
def sync_eventor_with_google_calendar():
    app.log.info('------method: sync-google-eventor')
    credentials_json_string = get_from_s3_safe(BUCKET, 'credentials', False)
    if not credentials_json_string:
        full_url = BASE_URL + '/authorize'
        app.log.debug('Credentials missing, redirecting to: ' + full_url)
        return make_redirect_response(BASE_URL + '/authorize')

    app.log.debug('Credentials exist, proceeding with google API request')
    cred_as_dict = json.loads(credentials_json_string)
    credentials = google.oauth2.credentials.Credentials(**cred_as_dict)

    calendar_client = googleapiclient.discovery.build(
        API_SERVICE_NAME, API_VERSION, credentials=credentials)

    processed_events = 0
    for calendar_config in CONFIG['calendar_config']:
        events = add_to_one_calendar(calendar_client, calendar_config['calendar_id'], calendar_config['organisation_ids'])
        processed_events += len(events)

    # Save credentials in case access token was refreshed.
    updated_credentials = credentials_to_dict(credentials)
    add_to_s3(BUCKET, 'credentials', json.dumps(updated_credentials))

    app.log.debug('Events synched successfully')

    return json.loads(json.dumps({'events_processed': processed_events}))


@app.schedule(Rate(60 * 24 * 7, unit=Rate.MINUTES))
def update_eventor_orgs_list():
    xml_text = get_from_eventor('/organisations')
    orgs_doc = untangle.parse(xml_text)

    orgs = orgs_doc.OrganisationList.Organisation
    orgs_json_str = '{\n'
    for org in orgs:
        orgs_json_str += '\t"' + org.OrganisationId.cdata + '": "' + org.Name.cdata + '",\n'

    # need to remove the trailing comma and \n after the final organisation
    orgs_json_str = orgs_json_str[:-2]

    orgs_json_str +='}'
    orgs_json = json.loads(orgs_json_str)

    add_to_s3(BUCKET, 'eventor-orgs', json.dumps(orgs_json))

    return json.loads(json.dumps({'organisations_found': len(orgs)}))


def add_to_one_calendar(calendar_client, calendar_id, organisation_ids):
    app.log.info('Start syncing events for organisation(s) ' + int_list_to_comma_sep_string(organisation_ids)
                 + ' => Calendar ' + calendar_id)
    existing_events = find_events(calendar_client, calendar_id)
    eventor_calendar_events = get_events_from_eventor(organisation_ids)
    for eventor_calendar_event in eventor_calendar_events:
        matching_calendar_event = event_already_exists(eventor_calendar_event, existing_events)
        if not matching_calendar_event:
            insert_event(calendar_client, eventor_calendar_event, calendar_id)
        else:
            if events_the_same(eventor_calendar_event, matching_calendar_event):
                app.log.debug('No changes to event - skip it.')
            else:
                patch_calendar_event(calendar_client, eventor_calendar_event, calendar_id, matching_calendar_event['id'])

    return eventor_calendar_events


def patch_calendar_event(calendar_client, updated_event, calendar_id, event_id):
    patched_event = calendar_client.events().patch(calendarId=calendar_id, eventId=event_id, body=updated_event).execute()
    app.log.info('Event patched: %s' % (patched_event.get('htmlLink')))


def events_the_same(eventor_calendar_event, matching_calendar_event):
    return eventor_calendar_event.items() <= matching_calendar_event.items()


def event_already_exists(event, existing_events):
    this_event_id = event['source']['title']
    for existing_event in existing_events:
        this_existing_source = existing_event.get('source', {'title': None})
        if this_event_id == this_existing_source.get('title'):
            app.log.debug('this event already exists - dont add it again: ' + this_event_id)
        app.log.debug('Eventor event: ' + json.dumps(event))
        app.log.debug('Calendar event: ' + json.dumps(existing_event))
        return existing_event
    app.log.debug('this event is new - add it: ' + this_event_id)
    return False


def format_utc(a_date):
    return a_date.isoformat() + 'Z' # 'Z' indicates UTC time


def get_future_date(from_date):
    return from_date + datetime.timedelta(weeks=+40)


def find_events(calendar_client, calendar_id):
    now = datetime.datetime.utcnow()
    future = get_future_date(now)
    app.log.info('Getting the upcoming events from Google Calendar, 40 weeks ahead, for calendar: ' + calendar_id)
    page_size = 50
    events_result = calendar_client.events().list(calendarId=calendar_id, timeMin=format_utc(now),
                                                  timeMax=format_utc(future), maxResults=page_size, singleEvents=True,
                                                  orderBy='startTime').execute()
    all_events = []
    while events_result.get('nextPageToken', False):
        app.log.debug('Fetching next page')
        next_page_token = events_result.get('nextPageToken', False)
        all_events.extend(events_result.get('items', []))
        events_result = calendar_client.events().list(calendarId=calendar_id, timeMin=format_utc(now),
                                              timeMax=format_utc(future), maxResults=page_size, singleEvents=True,
                                              orderBy='startTime', pageToken=next_page_token).execute()
    all_events.extend(events_result.get('items', []))
    app.log.info('Total events found: ' + str(len(all_events)))

    for event in all_events:
        app.log.debug(event['summary'])
        app.log.debug(event.get('source'))
    return all_events


def insert_event(calendar_service, event, calendar_id):
    event = calendar_service.events().insert(calendarId=calendar_id, body=event).execute()
    app.log.info('Event created: %s' % (event.get('htmlLink')))


@app.route('/authorize')
def authorize():
    app.log.info('------method: authorize')
    secrets_raw = get_from_s3(BUCKET, 'calendar-client-secrets')
    secrets = json.loads(secrets_raw)

    app.log.debug('secrets: ' + json.dumps(secrets))

    # Create flow instance to manage the OAuth 2.0 Authorization Grant Flow steps.
    flow = google_auth_oauthlib.flow.Flow.from_client_config(secrets, scopes=SCOPES)
    flow.redirect_uri = BASE_URL + '/oauth2callback'

    app.log.debug('redirect_uri: ' + flow.redirect_uri)
    authorization_url, state = flow.authorization_url(
        # Enable offline access so that you can refresh an access token without
        # re-prompting the user for permission. Recommended for web server apps.
        access_type='offline',
        # Enable incremental authorization. Recommended as a best practice.
        include_granted_scopes='true')

    app.log.debug('Current state: ' + state)
    app.log.debug('Current authorization url: ' + authorization_url)

    # Store the state so the callback can verify the auth server response.
    add_to_s3(BUCKET, 'state', state)
    return make_redirect_response(authorization_url)


@app.route('/oauth2callback')
def oauth2callback():
    app.log.info('------method: oauth2callback')
    # Specify the state when creating the flow in the callback so that it can
    # verified in the authorization server response.
    req_as_dict = app.current_request.to_dict()
    app.log.debug('state: ' + json.dumps(req_as_dict))

    state_raw = get_from_s3(BUCKET, 'state')
    state = state_raw.decode('utf-8')
    app.log.debug('state: ' + state)

    secrets = json.loads(get_from_s3(BUCKET, 'calendar-client-secrets'))

    # Create flow instance to manage the OAuth 2.0 Authorization Grant Flow steps.
    flow = google_auth_oauthlib.flow.Flow.from_client_config(
        secrets, scopes=SCOPES, state=state)
    # # flow.redirect_uri = flask.url_for('oauth2callback', _external=True)
    flow.redirect_uri = BASE_URL + '/oauth2callback'

    # Use the code in the authorization server's response to fetch the OAuth 2.0 tokens.
    flow.fetch_token(code=get_code_from_request())

    # Store credentials
    cred_dict = credentials_to_dict(flow.credentials)
    add_to_s3(BUCKET, 'credentials', json.dumps(cred_dict))

    return make_redirect_response(BASE_URL + '/sync-google-eventor')


def make_redirect_response(location):
    return Response(
        status_code=307,
        body='',
        headers={'Location': location, 'Content-Type': 'text/plain'})


def credentials_to_dict(credentials):
    return {'token': credentials.token,
            'refresh_token': credentials.refresh_token,
            'token_uri': credentials.token_uri,
            'client_id': credentials.client_id,
            'client_secret': credentials.client_secret,
            'scopes': credentials.scopes}


init()