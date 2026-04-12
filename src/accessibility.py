import concurrent.futures
import logging
import platform
import re
import subprocess
from typing import Any, Dict, List, Optional, Sequence, Tuple

import lxml.etree
from lxml.etree import _Element

from .platform_runtime import (
    ATAction,
    ATText,
    ATValue,
    Accessible,
    AppKit,
    ApplicationServices,
    Component,
    Desktop,
    HAS_MACOS_A11Y,
    HAS_PYATSPI,
    HAS_PYWINAUTO,
    Quartz,
    STATE_SHOWING,
    StateType,
    pyatspi,
)

logger = logging.getLogger(__name__)

_accessibility_ns_map = {
    "ubuntu": {
        "st": "https://accessibility.ubuntu.example.org/ns/state",
        "attr": "https://accessibility.ubuntu.example.org/ns/attributes",
        "cp": "https://accessibility.ubuntu.example.org/ns/component",
        "doc": "https://accessibility.ubuntu.example.org/ns/document",
        "docattr": "https://accessibility.ubuntu.example.org/ns/document/attributes",
        "txt": "https://accessibility.ubuntu.example.org/ns/text",
        "val": "https://accessibility.ubuntu.example.org/ns/value",
        "act": "https://accessibility.ubuntu.example.org/ns/action",
    },
    "windows": {
        "st": "https://accessibility.windows.example.org/ns/state",
        "attr": "https://accessibility.windows.example.org/ns/attributes",
        "cp": "https://accessibility.windows.example.org/ns/component",
        "doc": "https://accessibility.windows.example.org/ns/document",
        "docattr": "https://accessibility.windows.example.org/ns/document/attributes",
        "txt": "https://accessibility.windows.example.org/ns/text",
        "val": "https://accessibility.windows.example.org/ns/value",
        "act": "https://accessibility.windows.example.org/ns/action",
        "class": "https://accessibility.windows.example.org/ns/class",
    },
    "macos": {
        "st": "https://accessibility.macos.example.org/ns/state",
        "attr": "https://accessibility.macos.example.org/ns/attributes",
        "cp": "https://accessibility.macos.example.org/ns/component",
        "doc": "https://accessibility.macos.example.org/ns/document",
        "txt": "https://accessibility.macos.example.org/ns/text",
        "val": "https://accessibility.macos.example.org/ns/value",
        "act": "https://accessibility.macos.example.org/ns/action",
        "role": "https://accessibility.macos.example.org/ns/role",
    },
}

accessibility_ns_map_ubuntu = _accessibility_ns_map["ubuntu"]
accessibility_ns_map_windows = _accessibility_ns_map["windows"]
accessibility_ns_map_macos = _accessibility_ns_map["macos"]

libreoffice_version_tuple: Optional[Tuple[int, ...]] = None
MAX_DEPTH = 50
MAX_WIDTH = 1024
MAX_CALLS = 5000


def has_active_terminal(desktop: Accessible) -> bool:
    for app in desktop:
        if app.getRoleName() == "application" and app.name == "gnome-terminal-server":
            for frame in app:
                if frame.getRoleName() == "frame" and frame.getState().contains(pyatspi.STATE_ACTIVE):
                    return True
    return False


def get_terminal_output() -> Optional[str]:
    user_platform = platform.system()
    output: Optional[str] = None
    if user_platform != "Linux":
        raise NotImplementedError(f"Currently not implemented for platform {platform.platform()}.")
    if not HAS_PYATSPI:
        raise RuntimeError("pyatspi is not available on this machine.")

    desktop: Accessible = pyatspi.Registry.getDesktop(0)
    if has_active_terminal(desktop):
        desktop_xml: _Element = create_atspi_node(desktop)
        xpath = '//application[@name="gnome-terminal-server"]/frame[@st:active="true"]//terminal[@st:focused="true"]'
        terminals: List[_Element] = desktop_xml.xpath(xpath, namespaces=accessibility_ns_map_ubuntu)
        output = terminals[0].text.rstrip() if len(terminals) == 1 else None
    return output


def get_libreoffice_version() -> Tuple[int, ...]:
    result = subprocess.run("libreoffice --version", shell=True, text=True, stdout=subprocess.PIPE)
    version_str = result.stdout.split()[1]
    return tuple(map(int, version_str.split(".")))


def create_atspi_node(node: Accessible, depth: int = 0, flag: Optional[str] = None) -> _Element:
    node_name = node.name
    attribute_dict: Dict[str, Any] = {"name": node_name}

    states: List[StateType] = node.getState().get_states()
    for st in states:
        state_name: str = StateType._enum_lookup[st]
        state_name = state_name.split("_", maxsplit=1)[1].lower()
        if len(state_name) == 0:
            continue
        attribute_dict[f"{{{accessibility_ns_map_ubuntu['st']}}}{state_name}"] = "true"

    attributes: Dict[str, str] = node.get_attributes()
    for attribute_name, attribute_value in attributes.items():
        if len(attribute_name) == 0:
            continue
        attribute_dict[f"{{{accessibility_ns_map_ubuntu['attr']}}}{attribute_name}"] = attribute_value

    if (
        attribute_dict.get(f"{{{accessibility_ns_map_ubuntu['st']}}}visible", "false") == "true"
        and attribute_dict.get(f"{{{accessibility_ns_map_ubuntu['st']}}}showing", "false") == "true"
    ):
        try:
            component: Component = node.queryComponent()
        except NotImplementedError:
            pass
        else:
            bbox: Sequence[int] = component.getExtents(pyatspi.XY_SCREEN)
            attribute_dict[f"{{{accessibility_ns_map_ubuntu['cp']}}}screencoord"] = str(tuple(bbox[0:2]))
            attribute_dict[f"{{{accessibility_ns_map_ubuntu['cp']}}}size"] = str(tuple(bbox[2:]))

    text = ""
    try:
        text_obj: ATText = node.queryText()
        text = text_obj.getText(0, text_obj.characterCount)
        text = text.replace("\ufffc", "").replace("\ufffd", "")
    except NotImplementedError:
        pass

    try:
        node.queryImage()
        attribute_dict["image"] = "true"
    except NotImplementedError:
        pass

    try:
        node.querySelection()
        attribute_dict["selection"] = "true"
    except NotImplementedError:
        pass

    try:
        value: ATValue = node.queryValue()
        value_key = f"{{{accessibility_ns_map_ubuntu['val']}}}"
        for attr_name, attr_func in [
            ("value", lambda: value.currentValue),
            ("min", lambda: value.minimumValue),
            ("max", lambda: value.maximumValue),
            ("step", lambda: value.minimumIncrement),
        ]:
            try:
                attribute_dict[f"{value_key}{attr_name}"] = str(attr_func())
            except Exception:
                pass
    except NotImplementedError:
        pass

    try:
        action: ATAction = node.queryAction()
        for i in range(action.nActions):
            action_name: str = action.getName(i).replace(" ", "-")
            attribute_dict[f"{{{accessibility_ns_map_ubuntu['act']}}}{action_name}_desc"] = action.getDescription(i)
            attribute_dict[f"{{{accessibility_ns_map_ubuntu['act']}}}{action_name}_kb"] = action.getKeyBinding(i)
    except NotImplementedError:
        pass

    raw_role_name: str = node.getRoleName().strip()
    node_role_name = (raw_role_name or "unknown").replace(" ", "-")

    if not flag:
        if raw_role_name == "document spreadsheet":
            flag = "calc"
        if raw_role_name == "application" and node.name == "Thunderbird":
            flag = "thunderbird"

    xml_node = lxml.etree.Element(
        node_role_name,
        attrib=attribute_dict,
        nsmap=accessibility_ns_map_ubuntu,
    )

    if len(text) > 0:
        xml_node.text = text

    if depth == MAX_DEPTH:
        logger.warning("Max depth reached")
        return xml_node

    if flag == "calc" and node_role_name == "table":
        global libreoffice_version_tuple
        maximum_column = 1024 if libreoffice_version_tuple < (7, 4) else 16384
        max_row = 104_8576

        index_base = 0
        first_showing = False
        column_base = None
        for r in range(max_row):
            for clm in range(column_base or 0, maximum_column):
                child_node: Accessible = node[index_base + clm]
                showing: bool = child_node.getState().contains(STATE_SHOWING)
                if showing:
                    child_node_xml: _Element = create_atspi_node(child_node, depth + 1, flag)
                    if not first_showing:
                        column_base = clm
                        first_showing = True
                    xml_node.append(child_node_xml)
                elif first_showing and column_base is not None or clm >= 500:
                    break
            if first_showing and clm == column_base or not first_showing and r >= 500:
                break
            index_base += maximum_column
        return xml_node

    try:
        for i, ch in enumerate(node):
            if i == MAX_WIDTH:
                logger.warning("Max width reached")
                break
            xml_node.append(create_atspi_node(ch, depth + 1, flag))
    except Exception:
        logger.warning(
            "Error occurred during children traversing. Has Ignored. Node: %s",
            lxml.etree.tostring(xml_node, encoding="unicode"),
        )
    return xml_node


def create_pywinauto_node(node, nodes, depth: int = 0, flag: Optional[str] = None) -> _Element:
    nodes = nodes or set()
    if node in nodes:
        return
    nodes.add(node)

    attribute_dict: Dict[str, Any] = {"name": node.element_info.name}

    base_properties = {}
    try:
        base_properties.update(node.get_properties())
    except Exception:
        logger.debug("Failed to call get_properties(), trying to get writable properites")
        try:
            import pywinauto

            element_class = node.__class__

            class TempElement(node.__class__):
                writable_props = pywinauto.base_wrapper.BaseWrapper.writable_props

            node.__class__ = TempElement
            properties = node.get_properties()
            node.__class__ = element_class

            base_properties.update(properties)
            logger.debug("get writable properties")
        except Exception as exc:
            logger.error(exc)

    for attr_name in ["control_count", "button_count", "item_count", "column_count"]:
        try:
            attribute_dict[f"{{{accessibility_ns_map_windows['cnt']}}}{attr_name}"] = base_properties[attr_name].lower()
        except Exception:
            pass

    try:
        attribute_dict[f"{{{accessibility_ns_map_windows['cols']}}}columns"] = base_properties["columns"].lower()
    except Exception:
        pass

    for attr_name in ["control_id", "automation_id", "window_id"]:
        try:
            attribute_dict[f"{{{accessibility_ns_map_windows['id']}}}{attr_name}"] = base_properties[attr_name].lower()
        except Exception:
            pass

    for attr_name, attr_func in [
        ("enabled", lambda: node.is_enabled()),
        ("visible", lambda: node.is_visible()),
        ("minimized", lambda: node.is_minimized()),
        ("maximized", lambda: node.is_maximized()),
        ("normal", lambda: node.is_normal()),
        ("unicode", lambda: node.is_unicode()),
        ("collapsed", lambda: node.is_collapsed()),
        ("checkable", lambda: node.is_checkable()),
        ("checked", lambda: node.is_checked()),
        ("focused", lambda: node.is_focused()),
        ("keyboard_focused", lambda: node.is_keyboard_focused()),
        ("selected", lambda: node.is_selected()),
        ("selection_required", lambda: node.is_selection_required()),
        ("pressable", lambda: node.is_pressable()),
        ("pressed", lambda: node.is_pressed()),
        ("expanded", lambda: node.is_expanded()),
        ("editable", lambda: node.is_editable()),
        ("has_keyboard_focus", lambda: node.has_keyboard_focus()),
        ("is_keyboard_focusable", lambda: node.is_keyboard_focusable()),
    ]:
        try:
            attribute_dict[f"{{{accessibility_ns_map_windows['st']}}}{attr_name}"] = str(attr_func()).lower()
        except Exception:
            pass

    try:
        rectangle = node.rectangle()
        attribute_dict[f"{{{accessibility_ns_map_windows['cp']}}}screencoord"] = f"({rectangle.left:d}, {rectangle.top:d})"
        attribute_dict[f"{{{accessibility_ns_map_windows['cp']}}}size"] = f"({rectangle.width():d}, {rectangle.height():d})"
    except Exception as exc:
        logger.error("Error accessing rectangle: %s", exc)

    text: str = node.window_text()
    if text == attribute_dict["name"]:
        text = ""

    if hasattr(node, "select"):
        attribute_dict["selection"] = "true"

    for attr_name, attr_funcs in [
        ("step", [lambda: node.get_step()]),
        ("value", [lambda: node.value(), lambda: node.get_value(), lambda: node.get_position()]),
        ("min", [lambda: node.min_value(), lambda: node.get_range_min()]),
        ("max", [lambda: node.max_value(), lambda: node.get_range_max()]),
    ]:
        for attr_func in attr_funcs:
            if hasattr(node, attr_func.__name__):
                try:
                    attribute_dict[f"{{{accessibility_ns_map_windows['val']}}}{attr_name}"] = str(attr_func())
                    break
                except Exception:
                    pass

    attribute_dict[f"{{{accessibility_ns_map_windows['class']}}}class"] = str(type(node))

    for attr_name in ["class_name", "friendly_class_name"]:
        try:
            attribute_dict[f"{{{accessibility_ns_map_windows['class']}}}{attr_name}"] = base_properties[attr_name].lower()
        except Exception:
            pass

    node_role_name: str = node.class_name().lower().replace(" ", "-")
    node_role_name = "".join(
        map(lambda ch: ch if ch.isidentifier() or ch in {"-"} or ch.isalnum() else "-", node_role_name)
    )

    if node_role_name.strip() == "":
        node_role_name = "unknown"
    if not node_role_name[0].isalpha():
        node_role_name = "tag" + node_role_name

    xml_node = lxml.etree.Element(
        node_role_name,
        attrib=attribute_dict,
        nsmap=accessibility_ns_map_windows,
    )

    if text is not None and len(text) > 0 and text != attribute_dict["name"]:
        xml_node.text = text

    if depth == MAX_DEPTH:
        logger.warning("Max depth reached")
        return xml_node

    children = node.children()
    if children:
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future_to_child = [
                executor.submit(create_pywinauto_node, ch, nodes, depth + 1, flag)
                for ch in children[:MAX_WIDTH]
            ]
        try:
            xml_node.extend([future.result() for future in concurrent.futures.as_completed(future_to_child)])
        except Exception as exc:
            logger.error("Exception occurred: %s", exc)
    return xml_node


def create_axui_node(node, nodes: set = None, depth: int = 0, bbox: tuple = None):
    nodes = nodes or set()
    if node in nodes:
        return
    nodes.add(node)

    reserved_keys = {
        "AXEnabled": "st",
        "AXFocused": "st",
        "AXFullScreen": "st",
        "AXTitle": "attr",
        "AXChildrenInNavigationOrder": "attr",
        "AXChildren": "attr",
        "AXFrame": "attr",
        "AXRole": "role",
        "AXHelp": "attr",
        "AXRoleDescription": "role",
        "AXSubrole": "role",
        "AXURL": "attr",
        "AXValue": "val",
        "AXDescription": "attr",
        "AXDOMIdentifier": "attr",
        "AXSelected": "st",
        "AXInvalid": "st",
        "AXRows": "attr",
        "AXColumns": "attr",
    }
    attribute_dict = {}

    if depth == 0:
        bbox = (
            node["kCGWindowBounds"]["X"],
            node["kCGWindowBounds"]["Y"],
            node["kCGWindowBounds"]["X"] + node["kCGWindowBounds"]["Width"],
            node["kCGWindowBounds"]["Y"] + node["kCGWindowBounds"]["Height"],
        )
        app_ref = ApplicationServices.AXUIElementCreateApplication(node["kCGWindowOwnerPID"])

        attribute_dict["name"] = node["kCGWindowOwnerName"]
        if attribute_dict["name"] != "Dock":
            error_code, app_wins_ref = ApplicationServices.AXUIElementCopyAttributeValue(app_ref, "AXWindows", None)
            if error_code:
                logger.error("MacOS parsing %s encountered Error code: %d", app_ref, error_code)
        else:
            app_wins_ref = [app_ref]
        node = app_wins_ref[0]

    error_code, attr_names = ApplicationServices.AXUIElementCopyAttributeNames(node, None)
    if error_code:
        return

    value = None

    if "AXFrame" in attr_names:
        error_code, attr_val = ApplicationServices.AXUIElementCopyAttributeValue(node, "AXFrame", None)
        rep = repr(attr_val)
        x_value = re.search(r"x:(-?[\d.]+)", rep)
        y_value = re.search(r"y:(-?[\d.]+)", rep)
        w_value = re.search(r"w:(-?[\d.]+)", rep)
        h_value = re.search(r"h:(-?[\d.]+)", rep)
        type_value = re.search(r"type\s?=\s?(\w+)", rep)
        value = {
            "x": float(x_value.group(1)) if x_value else None,
            "y": float(y_value.group(1)) if y_value else None,
            "w": float(w_value.group(1)) if w_value else None,
            "h": float(h_value.group(1)) if h_value else None,
            "type": type_value.group(1) if type_value else None,
        }

        if not any(v is None for v in value.values()):
            x_min = max(bbox[0], value["x"])
            x_max = min(bbox[2], value["x"] + value["w"])
            y_min = max(bbox[1], value["y"])
            y_max = min(bbox[3], value["y"] + value["h"])

            if x_min > x_max or y_min > y_max:
                return

    role = None
    text = None

    for attr_name, ns_key in reserved_keys.items():
        if attr_name not in attr_names:
            continue

        if value and attr_name == "AXFrame":
            bb = value
            if not any(v is None for v in bb.values()):
                attribute_dict[f"{{{accessibility_ns_map_macos['cp']}}}screencoord"] = f"({int(bb['x']):d}, {int(bb['y']):d})"
                attribute_dict[f"{{{accessibility_ns_map_macos['cp']}}}size"] = f"({int(bb['w']):d}, {int(bb['h']):d})"
            continue

        error_code, attr_val = ApplicationServices.AXUIElementCopyAttributeValue(node, attr_name, None)
        full_attr_name = f"{{{accessibility_ns_map_macos[ns_key]}}}{attr_name}"

        if attr_name == "AXValue" and not text:
            text = str(attr_val)
            continue

        if attr_name == "AXRoleDescription":
            role = attr_val
            continue

        if not (
            isinstance(attr_val, ApplicationServices.AXUIElementRef)
            or isinstance(attr_val, (AppKit.NSArray, list))
        ):
            if attr_val is not None:
                attribute_dict[full_attr_name] = str(attr_val)

    node_role_name = role.lower().replace(" ", "_") if role else "unknown_role"

    xml_node = lxml.etree.Element(
        node_role_name,
        attrib=attribute_dict,
        nsmap=accessibility_ns_map_macos,
    )

    if text is not None and len(text) > 0:
        xml_node.text = text

    if depth == MAX_DEPTH:
        logger.warning("Max depth reached")
        return xml_node

    future_to_child = []

    with concurrent.futures.ThreadPoolExecutor() as executor:
        for attr_name, ns_key in reserved_keys.items():
            if attr_name not in attr_names:
                continue

            error_code, attr_val = ApplicationServices.AXUIElementCopyAttributeValue(node, attr_name, None)
            if isinstance(attr_val, ApplicationServices.AXUIElementRef):
                future_to_child.append(executor.submit(create_axui_node, attr_val, nodes, depth + 1, bbox))
            elif isinstance(attr_val, (AppKit.NSArray, list)):
                for child in attr_val:
                    future_to_child.append(executor.submit(create_axui_node, child, nodes, depth + 1, bbox))

        try:
            for future in concurrent.futures.as_completed(future_to_child):
                result = future.result()
                if result is not None:
                    xml_node.append(result)
        except Exception as exc:
            logger.error("Exception occurred: %s", exc)

    return xml_node


def build_accessibility_tree() -> str:
    os_name: str = platform.system()

    if os_name == "Linux":
        if not HAS_PYATSPI:
            raise RuntimeError("pyatspi is not available on this machine.")
        global libreoffice_version_tuple
        libreoffice_version_tuple = get_libreoffice_version()

        desktop: Accessible = pyatspi.Registry.getDesktop(0)
        xml_node = lxml.etree.Element("desktop-frame", nsmap=accessibility_ns_map_ubuntu)
        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = [executor.submit(create_atspi_node, app_node, 1) for app_node in desktop]
            for future in concurrent.futures.as_completed(futures):
                xml_tree = future.result()
                xml_node.append(xml_tree)
        return lxml.etree.tostring(xml_node, encoding="unicode")

    if os_name == "Windows":
        if not HAS_PYWINAUTO:
            raise RuntimeError("pywinauto is not available on this machine.")
        desktop: Desktop = Desktop(backend="uia")
        xml_node = lxml.etree.Element("desktop", nsmap=accessibility_ns_map_windows)
        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = [executor.submit(create_pywinauto_node, wnd, {}, 1) for wnd in desktop.windows()]
            for future in concurrent.futures.as_completed(futures):
                xml_tree = future.result()
                xml_node.append(xml_tree)
        return lxml.etree.tostring(xml_node, encoding="unicode")

    if os_name == "Darwin":
        if not HAS_MACOS_A11Y:
            raise RuntimeError("macOS accessibility dependencies are not available on this machine.")
        xml_node = lxml.etree.Element("desktop", nsmap=accessibility_ns_map_macos)

        with concurrent.futures.ThreadPoolExecutor() as executor:
            foreground_windows = [
                win
                for win in Quartz.CGWindowListCopyWindowInfo(
                    Quartz.kCGWindowListExcludeDesktopElements | Quartz.kCGWindowListOptionOnScreenOnly,
                    Quartz.kCGNullWindowID,
                )
                if win["kCGWindowLayer"] == 0 and win["kCGWindowOwnerName"] != "Window Server"
            ]
            dock_info = [
                win
                for win in Quartz.CGWindowListCopyWindowInfo(
                    Quartz.kCGWindowListOptionAll,
                    Quartz.kCGNullWindowID,
                )
                if win.get("kCGWindowName", None) == "Dock"
            ]

            futures = [executor.submit(create_axui_node, wnd, None, 0) for wnd in foreground_windows + dock_info]
            for future in concurrent.futures.as_completed(futures):
                xml_tree = future.result()
                if xml_tree is not None:
                    xml_node.append(xml_tree)

        return lxml.etree.tostring(xml_node, encoding="unicode")

    raise NotImplementedError(f"Currently not implemented for platform {platform.platform()}.")
