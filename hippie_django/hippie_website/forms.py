# No server-rendered forms remain.
#
# Network Query was the last Django form page; it moved to React in Batch 5 and
# now posts JSON to network_query_api (see views.py). All query pages are React
# entrypoints driven by the shared FilterBox, so this module is intentionally
# empty. Re-add forms here if a server-rendered form is ever reintroduced.
