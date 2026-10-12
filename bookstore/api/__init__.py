"""HTTP interface to the bookstore.

This package is a second presentation layer alongside the Tkinter desktop app.
It contains no business rules of its own: every endpoint delegates to the same
``RulesEngine``, ``Catalog``, ``Recommender`` and ``ChatService`` the desktop
app uses.

That is the point of it. The domain layer was written with no dependency on
the user interface, so adding a web front end meant writing transport and
nothing else -- no rule was copied, reimplemented or allowed to drift. If a
threshold changes in ``config/settings.json``, both interfaces change together
because there is only one implementation of each rule.
"""
