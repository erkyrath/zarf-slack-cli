#!/usr/bin/env python3

# http://python-prompt-toolkit.readthedocs.io/en/master/pages/building_prompts.html

import sys
import os
import time
import json
from collections import OrderedDict
import optparse
import asyncio
import threading
import prompt_toolkit
from slackclient import SlackClient

token_file = '.zlack-tokens'

popt = optparse.OptionParser(usage='slack-client.py [ OPTIONS ]')

(opts, args) = popt.parse_args()

thread = None
connections = OrderedDict()

def read_tokens():
    path = os.path.join(os.environ.get('HOME'), token_file)
    try:
        fl = open(path)
        dat = json.load(fl, object_pairs_hook=OrderedDict)
        fl.close()
        return dat
    except:
        return OrderedDict()

class ZarfSlackClient(SlackClient):
    def __init__(self, token, proxies=None, handler=None):
        SlackClient.__init__(self, token, proxies)
        self.server.websocket_safe_read = None
        self.message_handler = handler
        
    def api_call_check(self, method, **kwargs):
        res = self.api_call(method, **kwargs)
        if not res.get('ok'):
            msg = 'Slack error (%s): %s' % (method, res.get('error', '???'),)
            thread.add_output(msg)
            return None
        return res

    def rtm_disconnect(self):
        if self.server.websocket is not None:
            print('### closing websocket')
            self.server.websocket.send_close()
            self.server.websocket.close()
            self.server.websocket = None
        self.server.connected = False
        self.server.last_connected_at = 0

    def rtm_read(self):
        pass

class Connection():
    def __init__(self, id):
        self.id = id
        self.team = tokens[id]
        self.team_name = self.team['team_name']
        self.users = {}
        self.channels = {}
        self.client = ZarfSlackClient(self.team['access_token'], handler=self.handle_message)

    def handle_message(self, msg):
        thread.add_output('message: %s' % (msg,))

def get_next_cursor(res):
    metadata = res.get('response_metadata')
    if not metadata:
        return None
    return metadata.get('next_cursor', None)
    
def connect_to_teams():
    for id in tokens.keys():
        conn = Connection(id)
        connections[id] = conn
        
    for conn in connections.values():
        thread.add_output('Fetching users from %s' % (conn.team_name,))
        cursor = None
        while True:
            if thread.check_shutdown():
                return
            res = conn.client.api_call_check('users.list', cursor=cursor)
            if not res:
                break
            for user in res.get('members'):
                userid = user['id']
                username = user['profile']['display_name']
                userrealname = user['profile']['real_name']
                conn.users[userid] = (username, userrealname)
            cursor = get_next_cursor(res)
            if not cursor:
                break
        print('###', conn.users)

        thread.add_output('Fetching channels from %s' % (conn.team_name,))
        cursor = None
        while True:
            if thread.check_shutdown():
                return
            res = conn.client.api_call_check('channels.list', exclude_archived=True, exclude_members=True, cursor=cursor)
            if not res:
                break
            for chan in res.get('channels'):
                chanid = chan['id']
                channame = chan['name']
                conn.channels[chanid] = channame
            cursor = get_next_cursor(res)
            if not cursor:
                break
        print('###', conn.channels)

    for conn in connections.values():
        res = conn.client.rtm_connect(reconnect=True, with_team_state=False)
        print('### rtm_connect', res)
        print('### timeout', conn.client.server.websocket.gettimeout())

def disconnect_all_teams():
    for conn in connections.values():
        conn.client.rtm_disconnect()
    
class SlackThread(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self, name='slack-thread')
        self.input_list = []
        self.output_list = []
        self.want_shutdown = False
        self.lock = threading.Lock()
        self.cond = threading.Condition()
        
    def run(self):
        connect_to_teams()
        while not self.check_shutdown():
            ls = self.fetch_inputs()
            for ln in ls:
                self.add_output('Processed: ' + ln)
            with self.cond:
                self.cond.wait(5.0)
        disconnect_all_teams()
        self.add_output('Disconnected.')

    def set_shutdown(self):
        with self.cond:
            self.want_shutdown = True
            self.cond.notify()

    def check_shutdown(self):
        with self.lock:
            flag = self.want_shutdown
        return flag

    def add_input(self, val):
        with self.cond:
            self.input_list.append(val)
            self.cond.notify()
            
    def fetch_inputs(self):
        res = []
        with self.lock:
            if self.input_list:
                res.extend(self.input_list)
                self.input_list.clear()
        return res
    
    def add_output(self, val):
        with self.lock:
            self.output_list.append(val)
            
    def fetch_outputs(self):
        res = []
        with self.lock:
            if self.output_list:
                res.extend(self.output_list)
                self.output_list.clear()
        return res

async def input_loop():
    while thread.is_alive():
        try:
            input = await prompt_toolkit.prompt_async('>', patch_stdout=True)
            input = input.rstrip()
            if input:
                thread.add_input(input)
        except KeyboardInterrupt:
            print('<KeyboardInterrupt>')
            thread.set_shutdown()
        except EOFError:
            print('<EOFError>')
            thread.set_shutdown()

def check_for_outputs(evloop):
    ls = thread.fetch_outputs();
    for ln in ls:
        print(ln)
    if thread.is_alive():
        evloop.call_later(0.1, check_for_outputs, evloop)

tokens = read_tokens()

thread = SlackThread()
thread.start()

evloop = asyncio.get_event_loop()

evloop.call_soon(check_for_outputs, evloop)

evloop.run_until_complete(input_loop())
evloop.close()
