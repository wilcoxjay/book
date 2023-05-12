"""
This file compiles the code in Web Browser Engineering,
up to and including Chapter 16 (Reusing Previous Computations),
without exercises.
"""

import sdl2
import skia
import ctypes
from lab4 import print_tree
from lab4 import HTMLParser
from lab13 import Text, Element
from lab6 import resolve_url
from lab6 import tree_to_list
from lab6 import INHERITED_PROPERTIES
from lab6 import compute_style
from lab8 import layout_mode
from lab9 import EVENT_DISPATCH_CODE
from lab10 import COOKIE_JAR, url_origin
from lab11 import draw_text, get_font, linespace, \
    parse_blend_mode, CHROME_PX, SCROLL_STEP
import OpenGL.GL as GL
from lab12 import MeasureTime
from lab13 import diff_styles, \
    CompositedLayer, absolute_bounds, absolute_bounds_for_obj, \
    DrawCompositedLayer, Task, TaskRunner, SingleThreadedTaskRunner, \
    clamp_scroll, add_parent_pointers, \
    DisplayItem, DrawText, \
    DrawLine, paint_visual_effects, WIDTH, HEIGHT, INPUT_WIDTH_PX, \
    REFRESH_RATE_SEC, HSTEP, VSTEP, SETTIMEOUT_CODE, XHR_ONLOAD_CODE, \
    Transform, ANIMATED_PROPERTIES, SaveLayer
from lab14 import parse_color, parse_outline, draw_rect, DrawRRect, \
    is_focused, paint_outline, has_outline, \
    device_px, cascade_priority, style, \
    is_focusable, get_tabindex, announce_text, speak_text, \
    CSSParser, main_func
from lab15 import request, DrawImage, DocumentLayout, BlockLayout, \
    EmbedLayout, InputLayout, LineLayout, TextLayout, ImageLayout, \
    IframeLayout, JSContext, style, AccessibilityNode, Frame, Tab, \
    CommitData, draw_line, Browser, BROKEN_IMAGE, font, add_main_args
import wbetools

@wbetools.patch(Element)
class Element:
    def __init__(self, tag, attributes, parent):
        self.tag = tag
        self.attributes = attributes
        self.parent = parent

        self.animations = {}

        self.is_focused = False
        self.layout_object = None

        self.children_field = DependentField(self, "children")
        self.children = self.children_field.set([])
        self.style_field = DependentField(self, "style")
        self.style = {}

@wbetools.patch(Text)
class Text:
    def __init__(self, text, parent):
        self.text = text
        self.parent = parent

        self.animations = {}

        self.is_focused = False
        self.layout_object = None

        self.children_field = DependentField(self, "children")
        self.children = self.children_field.set([])
        self.style_field = DependentField(self, "style")
        self.style = {}

@wbetools.patch(is_focusable)
def is_focusable(node):
    if get_tabindex(node) <= 0:
        return False
    elif "tabindex" in node.attributes:
        return True
    elif "contenteditable" in node.attributes:
        return True
    else:
        return node.tag in ["input", "button", "a"]

@wbetools.patch(Frame)
class Frame:
    def load(self, url, body=None):
        self.zoom = 1
        self.scroll = 0
        self.scroll_changed_in_frame = True
        headers, body = request(url, self.url, payload=body)
        body = body.decode("utf8")
        self.url = url

        self.allowed_origins = None
        if "content-security-policy" in headers:
           csp = headers["content-security-policy"].split()
           if len(csp) > 0 and csp[0] == "default-src":
               self.allowed_origins = csp[1:]

        self.nodes = HTMLParser(body).parse()

        self.js = self.tab.get_js(url_origin(url))
        self.js.add_window(self)

        scripts = [node.attributes["src"] for node
                   in tree_to_list(self.nodes, [])
                   if isinstance(node, Element)
                   and node.tag == "script"
                   and "src" in node.attributes]
        for script in scripts:
            script_url = resolve_url(script, url)
            if not self.allowed_request(script_url):
                print("Blocked script", script, "due to CSP")
                continue

            header, body = request(script_url, url)
            body = body.decode("utf8")
            task = Task(
                self.js.run, script_url, body,
                self.window_id)
            self.tab.task_runner.schedule_task(task)

        self.rules = self.default_style_sheet.copy()
        links = [node.attributes["href"]
                 for node in tree_to_list(self.nodes, [])
                 if isinstance(node, Element)
                 and node.tag == "link"
                 and "href" in node.attributes
                 and node.attributes.get("rel") == "stylesheet"]
        for link in links:  
            style_url = resolve_url(link, url)
            if not self.allowed_request(style_url):
                print("Blocked style", link, "due to CSP")
                continue
            try:
                header, body = request(style_url, url)
            except:
                continue
            self.rules.extend(CSSParser(body.decode("utf8")).parse())

        images = [node
            for node in tree_to_list(self.nodes, [])
            if isinstance(node, Element)
            and node.tag == "img"]
        for img in images:
            try:
                src = img.attributes.get("src", "")
                image_url = resolve_url(src, self.url)
                assert self.allowed_request(image_url), \
                    "Blocked load of " + image_url + " due to CSP"
                header, body = request(image_url, self.url)
                img.encoded_data = body
                data = skia.Data.MakeWithoutCopy(body)
                img.image = skia.Image.MakeFromEncoded(data)
                assert img.image, "Failed to recognize image format for " + image_url
            except Exception as e:
                print("Exception loading image: url="
                    + image_url + " exception=" + str(e))
                img.image = BROKEN_IMAGE

        iframes = [node
                   for node in tree_to_list(self.nodes, [])
                   if isinstance(node, Element)
                   and node.tag == "iframe"
                   and "src" in node.attributes]
        for iframe in iframes:
            document_url = resolve_url(iframe.attributes["src"],
                self.tab.root_frame.url)
            if not self.allowed_request(document_url):
                print("Blocked iframe", document_url, "due to CSP")
                iframe.frame = None
                continue
            iframe.frame = Frame(self.tab, self, iframe)
            iframe.frame.load(document_url)

        # Changed---create DocumentLayout here
        self.document = DocumentLayout(self.nodes, self)
        self.set_needs_render()

        # For testing only?
        self.measure_layout = MeasureTime("layout")

    def keypress(self, char):
        if self.tab.focus and self.tab.focus.tag == "input":
            if not "value" in self.tab.focus.attributes:
                self.activate_element(self.tab.focus)
            if self.js.dispatch_event(
                "keydown", self.tab.focus, self.window_id): return
            self.tab.focus.attributes["value"] += char
            self.set_needs_render()
        elif self.tab.focus and "contenteditable" in self.tab.focus.attributes:
            text_nodes = [
                t for t in tree_to_list(self.tab.focus, [])
                if isinstance(t, Text)
            ]
            if text_nodes:
                last_text = text_nodes[-1]
            else:
                last_text = Text("", self.tab.focus)
                self.tab.focus.children.append(last_text)
            last_text.text += char
            self.tab.focus.children_field.notify()
            self.set_needs_render()

    def render(self):
        if self.needs_style:
            if self.tab.dark_mode:
                INHERITED_PROPERTIES["color"] = "white"
            else:
                INHERITED_PROPERTIES["color"] = "black"
            style(self.nodes,
                  sorted(self.rules,
                         key=cascade_priority), self)
            self.needs_layout = True
            self.needs_style = False

        if self.needs_layout:
            # Change here
            self.measure_layout.start_timing()
            self.document.layout(self.frame_width, self.tab.zoom)
            self.measure_layout.stop_timing()
            if self.tab.accessibility_is_on:
                self.tab.needs_accessibility = True
            else:
                self.needs_paint = True
            self.needs_layout = False

        clamped_scroll = self.clamp_scroll(self.scroll)
        if clamped_scroll != self.scroll:
            self.scroll_changed_in_frame = True
        self.scroll = clamped_scroll

@wbetools.patch(LineLayout)
class LineLayout:
    def __init__(self, node, parent, previous):
        self.node = node
        self.parent = parent
        self.previous = previous
        self.children = []

        self.x = None
        self.y = None
        self.width = None
        self.height = None

        self.zoom_field = DependentField(self, "zoom")
        self.x_field = DependentField(self, "x")
        self.y_field = DependentField(self, "y")
        self.width_field = DependentField(self, "width")
        self.height_field = DependentField(self, "height")
        self.parent.descendants.depend(self.node.children_field)
        self.parent.descendants.depend(self.node.style_field)
        self.parent.descendants.depend(self.zoom_field)
        self.parent.descendants.depend(self.width_field)
        self.parent.descendants.depend(self.height_field)
        self.parent.descendants.depend(self.x_field)
        self.parent.descendants.depend(self.y_field)

    def layout(self):
        parent_zoom = self.zoom_field.read(self.parent.zoom_field)
        self.zoom = self.zoom_field.set(parent_zoom)

        parent_width = self.width_field.read(self.parent.width_field)
        self.width = self.width_field.set(parent_width)

        parent_x = self.x_field.read(self.parent.x_field)
        self.x = self.x_field.set(parent_x)

        if self.previous:
            prev_y = self.y_field.read(self.previous.y_field)
            prev_height = self.y_field.read(self.previous.height_field)
            self.y = self.y_field.set(prev_y + prev_height)
        else:
            parent_y = self.y_field.read(self.parent.y_field)
            self.y = self.y_field.set(parent_y)

        for word in self.children:
            word.layout()

        if not self.children:
            self.height = self.height_field.set(0)
            return

        max_ascent = max([-child.get_ascent(1.25) 
                          for child in self.children])
        baseline = self.y + max_ascent
        for child in self.children:
            child.y = baseline + child.get_ascent()
        max_descent = max([child.get_descent(1.25)
                           for child in self.children])

        self.height = self.height_field.set(max_ascent + max_descent)
        
class DependentField:
    def __init__(self, base, name, eager=False):
        self.base = base
        self.name = name
        self.value = None
        self.dirty = True
        self.depended_on = set()
        self.eager = eager

    def depend(self, source):
        source.depended_on.add(self)
        self.dirty = True

    def read(self, field):
        assert not field.dirty, str(field)
        self.depend(field)
        return field.value

    def set(self, value):
        if value != self.value:
            self.notify()
            self.value = value
        self.dirty = False
        return value

    def mark(self):
        if not self.dirty:
            self.dirty = True
            if self.eager:
                self.notify()

    def notify(self):
        for field in self.depended_on:
            field.mark()

    def __str__(self):
        return str(self.base) + "." + self.name
        
@wbetools.patch(BlockLayout)
class BlockLayout:
    def __init__(self, node, parent, previous, frame):
        self.node = node
        node.layout_object = self
        self.parent = parent
        self.previous = previous
        self.children = []
        self.frame = frame

        if previous: previous.next = self
        self.next = None

        self.x = None
        self.y = None
        self.width = None
        self.height = None
        self.zoom = None

        self.children_field = DependentField(self, "children")
        self.zoom_field = DependentField(self, "zoom")
        self.width_field = DependentField(self, "width")
        self.x_field = DependentField(self, "x")
        self.y_field = DependentField(self, "y")
        self.height_field = DependentField(self, "height")
        self.descendants = DependentField(self, "descendants", eager=True)
        self.parent.descendants.depend(self.node.children_field)
        self.parent.descendants.depend(self.node.style_field)
        self.parent.descendants.depend(self.children_field)
        self.parent.descendants.depend(self.zoom_field)
        self.parent.descendants.depend(self.width_field)
        self.parent.descendants.depend(self.height_field)
        self.parent.descendants.depend(self.x_field)
        self.parent.descendants.depend(self.y_field)
        self.parent.descendants.depend(self.descendants)

    def layout(self):
        if self.zoom_field.dirty:
            parent_zoom = self.zoom_field.read(self.parent.zoom_field)
            self.zoom = self.zoom_field.set(parent_zoom)

        if self.width_field.dirty:
            node_style = self.width_field.read(self.node.style_field)
            if "width" in node_style:
                zoom = self.width_field.read(self.zoom_field)
                self.width = self.width_field.set(device_px(float(node_style["width"][:-2]), zoom))
            else:
                parent_width = self.width_field.read(self.parent.width_field)
                self.width = self.width_field.set(parent_width)

        if self.x_field.dirty:
            parent_x = self.x_field.read(self.parent.x_field)
            self.x = self.x_field.set(parent_x)

        if self.y_field.dirty:
            if self.previous: # Never changes
                prev_y = self.y_field.read(self.previous.y_field)
                prev_height = self.y_field.read(self.previous.height_field)
                self.y = self.y_field.set(prev_y + prev_height)
            else:
                parent_y = self.y_field.read(self.parent.y_field)
                self.y = self.y_field.set(parent_y)
            
        if self.children_field.dirty:
            node_children = self.children_field.read(self.node.children_field)
            mode = layout_mode(self.node)
            if mode == "block":
                self.children = []
                previous = None
                for child in node_children:
                    next = BlockLayout(child, self, previous, self.frame)
                    self.children.append(next)
                    previous = next
                self.children_field.set(self.children)
            else:
                self.children_field.read(self.node.style_field)
                self.children_field.read(self.width_field)
                self.children = []
                self.new_line()
                self.recurse(self.node)
                self.children_field.set(self.children)

        if self.descendants.dirty:
            for child in self.children:
                child.layout()
            self.descendants.set(None) # Reset to clean but do not notify

        if self.height_field.dirty:
            children = self.height_field.read(self.children_field)
            new_height = sum([
                self.height_field.read(child.height_field)
                for child in self.children
            ])
            self.height = self.height_field.set(new_height)

    def paint(self, display_list):
        assert not self.children_field.dirty
        
        cmds = []

        rect = skia.Rect.MakeLTRB(
            self.x, self.y, self.x + self.width,
            self.y + self.height)

        is_atomic = not isinstance(self.node, Text) and \
            (self.node.tag == "input" or self.node.tag == "button")

        if not is_atomic:
            bgcolor = self.node.style.get(
                "background-color", "transparent")
            if bgcolor != "transparent":
                radius = device_px(
                    float(self.node.style.get(
                        "border-radius", "0px")[:-2]),
                    self.zoom)
                cmds.append(DrawRRect(rect, radius, bgcolor))
 
        for child in self.children:
            child.paint(cmds)

        if self.node.is_focused and "contenteditable" in self.node.attributes:
            text_nodes = [
                t for t in tree_to_list(self, [])
                if isinstance(t, TextLayout)
            ]
            if text_nodes:
                cmds.append(DrawCursor(text_nodes[-1], text_nodes[-1].width))
            else:
                cmds.append(DrawCursor(self, 0))

        if not is_atomic:
            cmds = paint_visual_effects(self.node, cmds, rect)
        display_list.extend(cmds)

@wbetools.patch(InputLayout)
class InputLayout(EmbedLayout):
    def paint(self, display_list):
        cmds = []

        rect = skia.Rect.MakeLTRB(
            self.x, self.y, self.x + self.width,
            self.y + self.height)

        bgcolor = self.node.style.get("background-color",
                                 "transparent")
        if bgcolor != "transparent":
            radius = device_px(
                float(self.node.style.get("border-radius", "0px")[:-2]),
                self.zoom)
            cmds.append(DrawRRect(rect, radius, bgcolor))

        if self.node.tag == "input":
            text = self.node.attributes.get("value", "")
        elif self.node.tag == "button":
            if len(self.node.children) == 1 and \
               isinstance(self.node.children[0], Text):
                text = self.node.children[0].text
            else:
                print("Ignoring HTML contents inside button")
                text = ""

        color = self.node.style["color"]
        cmds.append(DrawText(self.x, self.y,
                             text, self.font, color))

        if self.node.is_focused and self.node.tag == "input":
            cmds.append(DrawCursor(self, self.font.measureText(text)))

        cmds = paint_visual_effects(self.node, cmds, rect)
        paint_outline(self.node, cmds, rect, self.zoom)
        display_list.extend(cmds)
        
def DrawCursor(elt, width):
    return DrawLine(elt.x + width, elt.y, elt.x + width, elt.y + elt.height)

@wbetools.patch(DocumentLayout)
class DocumentLayout:
    def __init__(self, node, frame):
        self.node = node
        self.frame = frame
        node.layout_object = self
        self.parent = None
        self.previous = None
        self.children = []

        self.zoom_field = DependentField(self, "zoom")
        self.width_field = DependentField(self, "width")
        self.height_field = DependentField(self, "height")
        self.x_field = DependentField(self, "x")
        self.y_field = DependentField(self, "y")
        self.descendants = DependentField(self, "descendants", eager=True)

        self.width = None
        self.height = None
        self.x = None
        self.y = None

    def layout(self, width, zoom):
        if not self.children:
            child = BlockLayout(self.node, self, None, self.frame)
        else:
            child = self.children[0]
        self.children = [child]

        self.zoom = self.zoom_field.set(zoom)
        self.width = self.width_field.set(width - 2 * device_px(HSTEP, zoom))
        self.x = self.x_field.set(device_px(HSTEP, zoom))
        self.y = self.y_field.set(device_px(VSTEP, zoom))
        if self.descendants.dirty:
            child.layout()
            self.descendants.set(None)
        child_height = self.height_field.read(child.height_field)
        self.height = self.height_field.set(child_height + 2*device_px(VSTEP, zoom))

@wbetools.patch(JSContext)
class JSContext:
    def innerHTML_set(self, handle, s, window_id):
        frame = self.tab.window_id_to_frame[window_id]        
        self.throw_if_cross_origin(frame)
        doc = HTMLParser(
            "<html><body>" + s + "</body></html>").parse()
        new_nodes = doc.children[0].children
        elt = self.handle_to_node[handle]
        for child in elt.children:
            child.parent = elt
        elt.children = elt.children_field.set(new_nodes)
        frame.set_needs_render()

@wbetools.patch(style)
def style(node, rules, frame):
    if node.style_field.dirty:
        old_style = node.style
        new_style = {}
    
        for property, default_value in INHERITED_PROPERTIES.items():
            if node.parent:
                parent_style = node.style_field.read(node.parent.style_field)
                new_style[property] = parent_style[property]
            else:
                new_style[property] = default_value
        for media, selector, body in rules:
            if media:
                if (media == "dark") != frame.tab.dark_mode: continue
            if not selector.matches(node): continue
            for property, value in body.items():
                if node.parent and property == "font-size" and value.endswith("%"):
                    node.style_field.read(node.parent.style_field)
                computed_value = compute_style(node, property, value)
                if not computed_value: continue
                new_style[property] = computed_value
        if isinstance(node, Element) and "style" in node.attributes:
            pairs = CSSParser(node.attributes["style"]).body()
            for property, value in pairs.items():
                if node.parent and property == "font-size" and value.endswith("%"):
                    node.style_field.read(node.parent.style_field)
                computed_value = compute_style(node, property, value)
                if not computed_value: continue
                new_style[property] = computed_value
    
        if old_style:
            transitions = diff_styles(old_style, new_style)
            for property, (old_value, new_value, num_frames) \
                in transitions.items():
                if property in ANIMATED_PROPERTIES:
                    frame.set_needs_render()
                    AnimationClass = ANIMATED_PROPERTIES[property]
                    animation = AnimationClass(
                        old_value, new_value, num_frames)
                    node.animations[property] = animation
                    new_style[property] = animation.animate()
    
        node.style = node.style_field.set(new_style)

    for child in node.children:
        style(child, rules, frame)

if __name__ == "__main__":
    args = add_main_args()
    main_func(args)
