# boot.py - runs first on every reset.
# Keep this minimal so REPL access stays reachable even if main.py crashes.
import sys
sys.path.append('/lib')
