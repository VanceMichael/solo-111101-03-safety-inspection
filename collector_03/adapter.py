from xml.etree import ElementTree


def decode(content):
    root = ElementTree.fromstring(content)
    return [dict(element.attrib, **{child.tag.split("}")[-1]: child.text or "" for child in element}) for element in root.findall(".//record")]
