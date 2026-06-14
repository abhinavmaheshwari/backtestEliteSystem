filename = "app/daily_builder.py"
with open(filename, 'r') as f:
    content = f.read()

bad_str = """    from config import WATCHLIST_PATH

# Globals for accumulation data
_DELIVERY_DATA = {}
_INST_BUYS = {}

    
    ist_now"""

good_str = """    from config import WATCHLIST_PATH
    
    global _DELIVERY_DATA, _INST_BUYS
    
    ist_now"""

content = content.replace(bad_str, good_str)

# Need to put the globals at the top of the file!
# Right after logger = logging.getLogger(__name__)

bad_globals = """logger = logging.getLogger(__name__)

# =====================================================================================
# OUTPUT FILES"""

good_globals = """logger = logging.getLogger(__name__)

# Globals for accumulation data
_DELIVERY_DATA = {}
_INST_BUYS = {}

# =====================================================================================
# OUTPUT FILES"""
content = content.replace(bad_globals, good_globals)

with open(filename, 'w') as f:
    f.write(content)
print("Indentation fixed")
