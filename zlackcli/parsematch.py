from enum import IntEnum

class ParseMatch:
    class Res(IntEnum):
        NONE = 0
        APPROX = 1
        EXACT = 2

    @staticmethod
    def nevermatch(text):
        return ParseMatch.Res.NONE
    
    @staticmethod
    def list_best(ls):
        Res = ParseMatch.Res

        best = None
        for res, val in ls:
            if res == Res.EXACT:
                return val
            if best is None and res == Res.APPROX:
                best = val
        return best
        
    @staticmethod
    def match_list(matchers, ls):
        Res = ParseMatch.Res

        if not matchers:
            raise Exception('match_list got empty lists')
        if len(ls) != len(matchers):
            raise Exception('match_list lengths differ')

        final = Res.EXACT
        for match, val in zip(matchers, ls):
            res = match(val)
            if not res:
                return Res.NONE
            if res == Res.APPROX:
                final = Res.APPROX
        return final
        
    def __init__(self, id, aliases=None):
        self.id = id.lower()
        self.aliases = None
        if aliases:
            self.aliases = set([ val.lower() for val in aliases ])

    def __repr__(self):
        ls = [ self.id ]
        if self.aliases:
            ls.extend(self.aliases)
        val = ','.join(ls)
        return '<ParseMatch "%s">' % (val,)

    def __call__(self, text):
        Res = ParseMatch.Res
        
        if self.id == text:
            return Res.EXACT
        if self.aliases and self.id in self.aliases:
            return Res.EXACT
        
        if text:
            if self.id.startswith(text):
                return Res.APPROX
            if self.aliases:
                for val in self.aliases:
                    if val.startswith(text):
                        return Res.APPROX
                    
        return Res.NONE
