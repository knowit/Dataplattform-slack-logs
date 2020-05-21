import boto3
from cachetools import cached, TTLCache
from datetime import datetime
from time import sleep
import requests
from os import environ

logs_client = boto3.client('logs')
ssm_client = boto3.client('ssm')

ssm_name_base = f'/{environ.get("STAGE")}/{environ.get("SERVICE")}'
tmp_last_query_time_ssm_name = f'{ssm_name_base}/tmp_last_query_time'
slack_callback_url_ssm_name = f'{ssm_name_base}/{environ.get("SLACK_CALLBACK_SSM_NAME")}'


@cached(cache=TTLCache(maxsize=1, ttl=1200))
def lambda_log_groups():
    log_groups = logs_client.describe_log_groups(
        logGroupNamePrefix=f'/aws/lambda/{environ.get("STAGE")}')['logGroups']
    return [x['logGroupName'] for x in log_groups]


@cached(cache=TTLCache(maxsize=1, ttl=1200))
def slack_callback_url():
    return ssm_client.get_parameter(
        Name=slack_callback_url_ssm_name,
        WithDecryption=False).get('Parameter', {}).get('Value', None)


def update_last_poll_time(query_time):
    ssm_client.put_parameter(
        Name=tmp_last_query_time_ssm_name,
        Value=str(query_time),
        Type='String',
        Overwrite=True,
        Tier='Standard')


def last_poll_time():
    try:
        return int(ssm_client.get_parameter(
            Name=tmp_last_query_time_ssm_name,
            WithDecryption=False).get('Parameter', {}).get('Value', 0))
    except:
        return 0


def handler(event, context):
    query_string = """
        FIELDS @timestamp, @message, @log
        | SORT @timestamp desc
        | PARSE @message "[*] *" as loggingType, loggingMessage
        | PARSE @log "*:/aws/lambda/*" as time, service
        | FILTER loggingType = "ERROR"
        | DISPLAY @timestamp, service, loggingMessage"""

    query_time = int(datetime.utcnow().timestamp())
    query_id = logs_client.start_query(
        logGroupNames=lambda_log_groups(),
        startTime=last_poll_time(),
        endTime=query_time,
        queryString=query_string,
        limit=50)['queryId']
    
    while True:
        query = logs_client.get_query_results(queryId=query_id)
        if query['status'] == 'Scheduled' or query['status'] == 'Running':
            sleep(0.1)
            continue

        if query['status'] != 'Complete':
            raise f'Cloudwatch query failed with status {query["status"]}'

        results = query['results']
        break

    if not results: 
        update_last_poll_time(query_time)
        return {'status_code': 200, 'body': ''}

    results = [
        {f['field']: f['value'] for f in x} for x in results
    ]

    attachments = [
        {
            'fallback': f'{res["service"]} @{res["@timestamp"]}: {res["loggingMessage"][0:10]}...',
            'pretext': f'{res["service"]} @{res["@timestamp"]}',
            'color': '#D00000',
            'fields':[
                {
                    'title': 'Logg message',
                    'value': res['loggingMessage'],
                    'short': False
                }
            ]
        }
        for res in reversed(results)
    ]

    requests.post(
        slack_callback_url(), 
        json={'attachments': attachments})

    update_last_poll_time(query_time)
    return {'status_code': 200, 'body': ''}


if __name__ == "__main__":
    handler(None, None)
