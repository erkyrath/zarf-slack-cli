#!/usr/bin/env python3

# http://python-prompt-toolkit.readthedocs.io/en/master/pages/building_prompts.html

import sys
import os
import re
import time
import json
from collections import OrderedDict
import optparse
import asyncio
import threading
import prompt_toolkit
from ssl import SSLError
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
        self.msg_counter = 0
        
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

    def rtm_send_json(self, msgtype, **kwargs):
        msg = dict(kwargs)
        msg['type'] = msgtype
        self.msg_counter += 1
        msg['id'] = self.msg_counter
        self.server.send_to_websocket(msg)

    def rtm_read(self):
        if self.server.websocket is None:
            return
        try:
            dat = self.server.websocket.recv()
            msg = None
            try:
                msg = json.loads(dat)
            except:
                thread.add_output('Websocket error: non-json message: %s' % (dat,))
            if msg is not None:
                self.message_handler(msg)
        except SSLError as ex:
            if ex.errno == 2:
                return
            raise

class Connection():
    def __init__(self, id):
        self.id = id
        self.team = tokens[id]
        self.team_name = self.team['team_name']
        self.user_id = self.team['user_id']
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
        ### if not res, close connection

def read_connections():
    for conn in connections.values():
        conn.client.rtm_read()
    
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
                #self.add_output('Processed: ' + ln)
                conn = connections['T03UD0D0X']
                conn.client.rtm_send_json('message', user=conn.user_id, channel='C03UD0D19', text=ln)
            read_connections()
            with self.cond:
                self.cond.wait(0.1)
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

# (teamid, channelid) for the current default channel
curchannel = None

pat_special_command = re.compile('/([a-z0-9_-]+)', flags=re.IGNORECASE)
pat_channel_command = re.compile(':([a-z0-9_-]+)(?:[/:]([a-z0-9_-]+))?', flags=re.IGNORECASE)

def handle_input(val):
    global curchannel
    match = pat_special_command.match(val)
    if match:
        cmd = match.group(1)
        val = val[ match.end() : ]
        val = val.lstrip()
        print('Special command not recognized:', cmd)
        return
    match = pat_channel_command.match(val)
    if match:
        val = val[ match.end() : ]
        val = val.lstrip()
        if match.group(2) is not None:
            # command ":TEAM/CHANNEL"
            team = parse_team(match.group(1))
            if not team:
                print('Team not recognized:', match.group(1))
                return
            teamid = team['team_id']
            channame = match.group(2)
        else:
            # command ":CHANNEL"
            if not curchannel:
                print('No current team.')
                return
            teamid = curchannel[0]
            channame = match.group(1)
        conn = connections.get(teamid)
        if not conn:
            print('Team not connected:', team_name(teamid))
            return
        chanid = parse_channel(conn, channame)
        if not chanid:
            print('Channel not recognized:', channame)
            return
        curchannel = (teamid, chanid)
    if not val:
        return
    if not curchannel:
        print('No current channel.')
        return
    print('###', curchannel, repr(val))    
    

def parse_team(val):
    for team in tokens.values():
        if team.get('team_id') == val:
            return team
        if team.get('team_name').startswith(val):
            return team
        alias = team.get('alias')
        if alias and val in alias:
            return team
    return None

def parse_channel(conn, val):
    for (chanid, name) in conn.channels.items():
        if val == chanid or val == name:
            return chanid
    for (chanid, name) in conn.channels.items():
        if name.startswith(val):
            return chanid
    return None
        
def team_name(teamid):
    if teamid not in tokens:
        return '???'+teamid
    return tokens[teamid]['team_name']

def channel_name(teamid, chanid):
    if teamid not in connections:
        return '???'+chanid
    conn = connections[teamid]
    if chanid not in conn.channels:
        return '???'+chanid
    return conn.channels[chanid]

async def input_loop():
    while thread.is_alive():
        try:
            prompt = '> '
            if curchannel:
                (teamid, chanid) = curchannel
                prompt = '%s/%s> ' % (team_name(teamid), channel_name(teamid, chanid))
            input = await prompt_toolkit.prompt_async(prompt, patch_stdout=True)
            input = input.rstrip()
            if input:
                handle_input(input)
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
