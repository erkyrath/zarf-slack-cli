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

    def team_get(self, team, key, defval=None):
        if isinstance(team, Team):
            team = team.key
        map = self.map['teams'].get(team)
        if map is None:
            return defval
        return map.get(key, defval)

    def team_put(self, team, key, val):
        if isinstance(team, Team):
            team = team.key
        map = self.map['teams'].get(team)
        if map is None:
            map = OrderedDict()
            self.map['teams'][team] = map
        map[key] = val
        self.mark_dirty()

    def mark_dirty(self):
        if self.write_handle:
            self.write_handle.cancel()
        self.write_handle = self.client.evloop.call_later(self.DELAY, self.write_if_dirty)

    def write_if_dirty(self):
        if self.write_handle:
            self.write_handle.cancel()
            self.write_handle = None
            self.write_file()
            

from .teamdat import Team
