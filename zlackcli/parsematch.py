from enum import IntEnum

class ParseMatch:
    """When parsing user commands, we have to do a lot of partial matching
    (against team and channel names). The ParseMatch class wraps this up
    in a handy interface.
    """
    
    class Res(IntEnum):
        NONE = 0
        APPROX = 1
        EXACT = 2

    @staticmethod
    def list_best(ls):
        """Given a list of (result, value) pairs, return the first value
        whose result is EXACT. If there are none, return the first value
        whose value is APPROX.
        """
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
        """Given a list of ParseMatch objects and an (equal) list of
        strings, check them elementwise. Return the worst result.

        (That is, if all results are EXACT, this returns EXACT. If they're
        a mix of EXACT and APPROX, this returns APPROX. If any are NONE,
        this returns NONE.)
        """
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
        """id is the name to match against; aliases is an optional collection
        of aliases.
        """
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
        """self(text) checks whether the text matches the name or any of
        the aliases. An exact match returns Res.EXACT. If the text is
        a (non-empty) prefix, it returns Res.APPROX. Otherwise it returns
        Res.NONE.

        The text argument *must* be lowercased. (We don't do this because
        it would be terrible inefficient. So it's the caller's job.)
        """
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


# NeverMatch is a ParseMatch which never matches. This is meant to be
# used as a default value.

class NeverMatch(ParseMatch):
    def __init__(self):
        pass
    def __repr__(self):
        return '<ParseMatch NeverMatch>'
    def __call__(self, text):
        return ParseMatch.Res.NONE
