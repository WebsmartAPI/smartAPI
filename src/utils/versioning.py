'''
Backup es index to S3 and refresh
'''
import logging

from data import SmartAPIData

def backup_and_refresh():
    '''
    Run periodically in the main event loop
    '''
    data = SmartAPIData()
    try:
        data.backup_all(aws_s3_bucket='smartapi')
    except Exception:
        logging.exception("Backup failed.")
    try:
        data.refresh_all(dryrun=False)
    except Exception:
        logging.exception("Refresh failed.")
