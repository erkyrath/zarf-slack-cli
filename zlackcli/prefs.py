import os
from collections import OrderedDict


class Prefs:
    def __init__(self, client, path):
        self.client = client
        self.path = path
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
            json.dump(fl, self.map)
            fl.close()
        except Exception as ex:
            self.client.print_exception(ex, 'Writing prefs')

    def get(self, key, defval=None):
        return self.map.get(key, defval)

    def teamget(self, team, key, defval=None):
        if isinstance(team, Team):
            team = team.id
        map = self.map['teams'].get(team)
        if map is None:
            return defval
        return map.get(key, defval)

from .teamdat import Team
