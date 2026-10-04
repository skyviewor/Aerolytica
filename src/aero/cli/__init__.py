"""Initialize terminal protocol settings before importing the TUI libraries."""

import os
import sys

# Kitty's enhanced keyboard protocol can interfere with Chinese IME commits.
if os.environ.get("AERO_ENABLE_KITTY_KEY") != "1":
    os.environ["TEXTUAL_DISABLE_KITTY_KEY"] = "1"

# Web serving must not probe terminal image escape sequences.
if len(sys.argv) > 1 and sys.argv[1] == "serve":
    os.environ["AERO_TEXTUAL_IMAGE_PROTOCOL"] = "0"
