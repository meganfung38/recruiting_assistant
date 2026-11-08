"""Walk Streamlit's headless element tree in render order, for 'everything on the page' assertions."""


def walk(block, out=None, depth=0):
    """[(depth, element type, text)] for every element under `block`, including collapsed expanders' contents."""
    out = [] if out is None else out
    for _, el in sorted(block.children.items(), key=lambda kv: int(kv[0])):
        t = type(el).__name__
        if getattr(el, "children", None) and t != "Dataframe":
            out.append((depth, t, str(getattr(el, "label", "") or getattr(el, "name", "") or "")))
            walk(el, out, depth + 1)
        else:
            val = getattr(el, "value", None)
            if t == "Dataframe" and val is not None:
                val = val.to_string()
            out.append((depth, t, str(val) if val is not None else str(getattr(el, "label", ""))))
    return out


def whole_page(at):
    """Main area and sidebar together."""
    return walk(at.main) + walk(at.sidebar)


def visible_by_default(block):
    """[(type, text)] for elements NOT inside a collapsed expander: what a recruiter sees without clicking anything."""
    out, stack = [], []
    for depth, t, v in walk(block):
        while stack and depth <= stack[-1]:
            stack.pop()
        if t == "Expander":
            stack.append(depth)
            continue
        if not stack:
            out.append((t, v))
    return out


def inside_expander(block, label_prefix):
    """[(type, text)] for elements inside the expander whose label starts with `label_prefix`."""
    out, stack = [], []
    for depth, t, v in walk(block):
        while stack and depth <= stack[-1]:
            stack.pop()
        if t == "Expander" and v.startswith(label_prefix):
            stack.append(depth)
            continue
        if stack:
            out.append((t, v))
    return out
