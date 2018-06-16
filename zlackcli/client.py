import os
from collections import OrderedDict

class ZlackClient:
    
    def __init__(self, tokenpath):
        self.teams = OrderedDict()
        self.tokenpath = tokenpath
        
        self.read_teams()
        if not self.teams:
            print('You are not authorized in any Slack groups. Type /auth to join one.')
        
    def read_teams(self):
        """Read the current token list from ~/.zlack-tokens.
        Return a dict of Team objects.
        """
        try:
            fl = open(self.tokenpath)
            dat = json.load(fl, object_pairs_hook=OrderedDict)
            fl.close()
        except:
            return
        for (id, map) in dat.items():
            self.teams[id] = Team(map)
    
    
