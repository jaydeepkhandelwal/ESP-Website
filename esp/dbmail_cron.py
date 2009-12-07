#!/usr/bin/python

import sys
sys.path += ['/esp/web/queens/']
sys.path += ['/esp/web/queens/esp/']
sys.path += ['/esp/web/queens/django/']

import os
os.environ['DJANGO_SETTINGS_MODULE'] = 'esp.settings'

import esp.manage
from esp.dbmail.cronmail import send_miniblog_messages, process_messages, send_email_requests
#send_event_notices_for_day('tomorrow')
#send_miniblog_messages()
process_messages()
send_email_requests()
