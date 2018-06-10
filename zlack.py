#!/usr/bin/env python

import sys
import os
import re
import time
import json
from collections import OrderedDict
import optparse
import traceback
import asyncio
import aiohttp

token_file = '.zlack-tokens'
debug_exceptions = True

def read_teams():
    """Read the current token list from ~/.zlack-tokens.
    Return a dict of Team objects.
    """
    path = os.path.join(os.environ.get('HOME'), token_file)
    try:
        fl = open(path)
        dat = json.load(fl, object_pairs_hook=OrderedDict)
        fl.close()
    except:
        dat = OrderedDict()
    res = OrderedDict()
    for (id, map) in dat.items():
        res[id] = Team(map)
    return res

class Team:
    """Represents one Slack group (team, workspace... I'm not all that
    consistent about it, sorry). This includes the websocket (which
    carries the RTM protocol). It also includes information about the
    group's channels and users.
    """
    domain = 'slack.com'
    
    def __init__(self, map):
        self.id = map['team_id']
        self.team_name = map.get('team_name', '???')
        self.user_id = map['user_id']
        self.access_token = map['access_token']
        if 'alias' in map:
            self.alias = list(map['alias'])
        else:
            self.alias = []
        self.origmap = map

        self.users = {}
        self.users_by_display_name = {}
        self.channels = {}
        self.channels_by_name = {}
        self.muted_channels = set()
        self.lastchannel = None
        
        self.session = None

    def __repr__(self):
        return '<Team %s "%s">' % (self.id, self.team_name)

    async def api_call(self, method, **kwargs):
        url = 'https://{0}/api/{1}'.format(self.domain, method)
        
        data = {}
        for (key, val) in kwargs.items():
            if val is None:
                continue
            ### channels, users, types: convert list to comma-separated string
            ### other lists/dicts: convert to json.dumps()
            data[key] = val
            
        async with self.session.post(url, data=data) as resp:
            return await resp.json()
    
    async def api_call_check(self, method, **kwargs):
        """Make a web API call. Return the result.
        On error, print an error message and return None.
        """
        try:
            res = await self.api_call(method, **kwargs)
            if res is None or not res.get('ok'):
                print('Slack error (%s): %s' % (method, res.get('error', '???'),))
                return None
            return res
        except Exception as ex:
            print('Slack exception (%s): %s: %s' % (method, ex.__class__.__name__, ex,))
            if debug_exceptions:
                traceback.print_exc()
            return None
        
    async def load_connection_data(self):
        """Load all the information we need for a connection: the channel
        and user lists.
        """
        
        print('Fetching user information for %s' % (self.team_name,))

        self.muted_channels.clear()
        self.channels.clear()
        self.users.clear()
    
        # The muted_channels information is stored in your Slack preferences,
        # which are an undocumented (but I guess widely used) API call.
        # See: https://github.com/ErikKalkoken/slackApiDoc
        res = await self.api_call_check('users.prefs.get')
        if res:
            prefs = res.get('prefs')
            mutels = prefs.get('muted_channels')
            if mutels:
                self.muted_channels = set(mutels.split(','))

        # Fetch user lists
        cursor = None
        while True:
            res = await self.api_call_check('users.list', cursor=cursor)
            if not res:
                break
            for user in res.get('members'):
                userid = user['id']
                username = user['profile']['display_name']
                if not username:
                    username = user['name']    # legacy data field
                userrealname = user['profile']['real_name']
                self.users[userid] = User(self, userid, username, userrealname)
                self.users_by_display_name[username] = self.users[userid]
            cursor = get_next_cursor(res)
            if not cursor:
                break
        #print(self.users)
    
        # Fetch public and private channels
        cursor = None
        while True:
            res = await self.api_call_check('conversations.list', exclude_archived=True, types='public_channel,private_channel', cursor=cursor)
            if not res:
                break
            for chan in res.get('channels'):
                chanid = chan['id']
                channame = chan['name']
                priv = chan['is_private']
                member = chan['is_member']
                self.channels[chanid] = Channel(self, chanid, channame, private=priv, member=member)
                self.channels_by_name[channame] = self.channels[chanid]
            cursor = get_next_cursor(res)
            if not cursor:
                break
            
        # Fetch IM (person-to-person) channels
        cursor = None
        while True:
            res = await self.api_call_check('conversations.list', exclude_archived=True, types='im', cursor=cursor)
            if not res:
                break
            for chan in res.get('channels'):
                chanid = chan['id']
                chanuser = chan['user']
                if chanuser in self.users:
                    self.users[chanuser].im_channel = chanid
                    channame = '@'+self.users[chanuser].name
                    self.channels[chanid] = Channel(self, chanid, channame, private=True, member=True, im=chanuser)
                    # But not channels_by_name.
            cursor = get_next_cursor(res)
            if not cursor:
                break

        

class Channel:
    """Simple object representing one channel in a group.
    """
    def __init__(self, team, id, name, private=False, member=True, im=None):
        self.team = team
        self.id = id
        self.name = name
        self.private = private
        self.member = member
        self.imuser = im

    def __repr__(self):
        if self.imuser:
            privflag = ' (im)'
        else:
            privflag = (' (priv)' if self.private else '')
        memberflag = (' (mem)' if self.member else '')
        return '<Channel %s%s%s: "%s">' % (self.id, privflag, memberflag, self.name)

    def muted(self):
        """Check whether this channel is muted. The mute flag is stored
        in the Team, because it comes from Slack's preferences data,
        not the channel data.
        """
        return (self.id in self.team.muted_channels)
    
class User:
    """Simple object representing one user in a group.
    """
    def __init__(self, team, id, name, real_name):
        self.team = team
        self.id = id
        self.name = name
        self.real_name = real_name
        self.im_channel = None  # May be set later
        
    def __repr__(self):
        return '<User %s: "%s"/"%s">' % (self.id, self.name, self.real_name)

def get_next_cursor(res):
    """Extract the next_cursor field from a message object. This is
    used by all Web API calls which get paginated results.
    """
    metadata = res.get('response_metadata')
    if not metadata:
        return None
    return metadata.get('next_cursor', None)
    

teams = read_teams()
if not teams:
    print('You are not authorized in any Slack groups.')
    sys.exit(-1)

async def create_all_sessions():
    for team in teams.values():
        headers = {
            'user-agent': 'zlack-test', ###versions
            'Authorization': 'Bearer {}'.format(team.access_token)
        }
        team.session = aiohttp.ClientSession(headers=headers)

async def shutdown_all():
    for team in teams.values():
        if team.session:
            await team.session.close()
            team.session = None

async def main():
    await create_all_sessions()
    (done, pending) = await asyncio.wait([ team.load_connection_data() for team in teams.values() ])
    for res in done:
        ex = res.exception()
        if ex is not None:
            print('could not load data: %s: %s' % (ex.__class__.__name__, ex))
            if debug_exceptions:
                traceback.print_tb(ex.__traceback__)
    await shutdown_all()

loop = asyncio.get_event_loop()
loop.run_until_complete(main())
