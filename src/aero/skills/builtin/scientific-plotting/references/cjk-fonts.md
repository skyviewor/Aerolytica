# CJK Fonts

Use this reference when Chinese, Japanese, or Korean text in matplotlib figures renders as tofu boxes (□□□).

## Checklist

- If CJK characters render as tofu boxes / empty squares, do NOT try to manually download font files or configure `matplotlibrc`.
- The recommended fix is `pip install mplfonts && mplfonts init`. This package registers CJK-capable fonts with matplotlib automatically.
- After running `mplfonts init`, restart the Python kernel or re-import matplotlib for the font cache to refresh.
- In generated plotting scripts containing CJK text, still call `from mplfonts import use_font` followed by `use_font("Noto Sans CJK SC")`. This makes the selected font explicit and prevents later `rcParams` changes from silently restoring a non-CJK font.
- Do not hard-code operating-system font paths or overwrite `font.sans-serif` with an unverified list.
- mplfonts project: https://github.com/Clarmy/mplfonts
- Do not use other CJK font solutions unless the user explicitly requests them.
