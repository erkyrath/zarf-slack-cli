import os
import json
from collections import OrderedDict


class Prefs:
    DELAY = 5
    
    def __init__(self, client, path):
        self.client = client
        self.path = path
        self.write_handle = None
        self.map = self.read_file()
        if 'teams' not in self.map:
            self.map['teams'] = OrderedDict()

    def read_file(self):
        try:
            fl = open(self.path)
            dat = json.load(fl, object_pairs_hook=OrderedDict)
            fl.close()
        except:
            dat = OrderedDict()
        return dat

    def write_file(self):
        try:
            fl = open(self.path, 'w')
            json.dump(self.map, fl, indent=1)
            fl.write('\n')
            fl.close()
        except Exception as ex:
            self.client.print_exception(ex, 'Writing prefs')

    def get(self, key, defval=None):
        return self.map.get(key, defval)

    def put(self, key, val):
        self.map[key] = val
        self.mark_dirty()

    def team_get(self, key, team, defval=None):
        if isinstance(team, Team):
            team = team.key
        map = self.map['teams'].get(team)
        if map is None:
            return defval
        return map.get(key, defval)

    def team_put(self, key, val, team):
        if isinstance(team, Team):
            team = team.key
        map = self.map['teams'].get(team)
        if map is None:
            map = OrderedDict()
            self.map['teams'][team] = map
        map[key] = val
        self.mark_dirty()

    def channel_get(self, key, team, chan, defval=None):
        if isinstance(team, Team):
            team = team.key
        if isinstance(chan, Channel):
            chan = chan.id
        map = self.map['teams'].get(team)
        if map is None:
            return defval
        chanmap = map.get('channels')
        if chanmap is None:
            return defval
        submap = chanmap.get(chan)
        if submap is None:
            return defval
        return submap.get(key, defval)

    def channel_put(self, key, val, team, chan):
        if isinstance(team, Team):
            team = team.key
        if isinstance(chan, Channel):
            chan = chan.id
        map = self.map['teams'].get(team)
        if map is None:
            map = OrderedDict()
            self.map['teams'][team] = map
        chanmap = map.get('channels')
        if chanmap is None:
            chanmap = OrderedDict()
            map['channels'] = chanmap
        submap = chanmap.get(chan)
        if submap is None:
            submap = OrderedDict()
            chanmap[chan] = submap
        submap[key] = val
        self.mark_dirty()

    def tree_get(self, key, team=None, chan=None, defval=None):
        if team is not None and chan is not None:
            val = self.channel_get(key, team, chan)
            if val is not None:
                return val
        if team is not None:
            val = self.team_get(key, team)
            if val is not None:
                return val
        return self.get(key, defval)

    def mark_dirty(self):
        if self.write_handle:
            self.write_handle.cancel()
        self.write_handle = self.client.evloop.call_later(self.DELAY, self.write_if_dirty)

    def write_if_dirty(self):
        if self.write_handle:
            self.write_handle.cancel()
            self.write_handle = None
            self.write_file()
            

from .teamdat import Team, Channel
