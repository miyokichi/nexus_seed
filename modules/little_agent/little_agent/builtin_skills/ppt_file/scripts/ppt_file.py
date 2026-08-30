from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tool = str(payload.get("tool") or (sys.argv[1] if len(sys.argv) > 1 else ""))
        workspace = Path(str(payload["workspace"])).resolve()
        arguments = payload.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object.")

        if tool == "read_ppt":
            result = read_ppt(workspace, arguments)
        elif tool == "write_ppt":
            result = write_ppt(workspace, arguments)
        else:
            result = {"ok": False, "content": f"Unknown PowerPoint tool: {tool}"}
    except Exception as exc:  # noqa: BLE001 - serialized for the host agent.
        result = {"ok": False, "content": f"PowerPoint script failed: {exc}"}

    print(json.dumps(result, ensure_ascii=False))
    return 0


def read_ppt(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    path = safe_path(workspace, str(arguments["path"]), ".pptx")
    max_slides = int(arguments.get("max_slides") or 50)
    if not path.exists():
        return {"ok": False, "content": f"File not found: {path.relative_to(workspace)}"}

    with zipfile.ZipFile(path) as deck:
        slide_paths = sorted(
            [name for name in deck.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", name)],
            key=slide_number,
        )
        sections = []
        for index, slide_path in enumerate(slide_paths[:max_slides], start=1):
            root = ET.fromstring(deck.read(slide_path))
            texts = [node.text or "" for node in root.findall(f".//{{{DRAWING_NS}}}t")]
            body = "\n".join(text for text in texts if text.strip())
            sections.append(f"# Slide {index}\n{body}".rstrip())

    return {"ok": True, "content": "\n\n".join(sections) if sections else "(no slides)"}


def write_ppt(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    path = safe_path(workspace, str(arguments["path"]), ".pptx")
    slides = normalize_slides(arguments.get("slides"))
    if not slides:
        return {"ok": False, "content": "At least one slide is required."}
    path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as deck:
        deck.writestr("[Content_Types].xml", content_types(len(slides)))
        deck.writestr("_rels/.rels", root_rels())
        deck.writestr("docProps/app.xml", app_props(len(slides)))
        deck.writestr("docProps/core.xml", core_props())
        deck.writestr("ppt/presentation.xml", presentation_xml(len(slides)))
        deck.writestr("ppt/_rels/presentation.xml.rels", presentation_rels(len(slides)))
        deck.writestr("ppt/presProps.xml", empty_part("p:presentationPr"))
        deck.writestr("ppt/viewProps.xml", empty_part("p:viewPr"))
        deck.writestr("ppt/theme/theme1.xml", theme_xml())
        for index, slide in enumerate(slides, start=1):
            deck.writestr(f"ppt/slides/slide{index}.xml", slide_xml(index, slide))

    return {"ok": True, "content": f"Wrote {path.relative_to(workspace)} ({len(slides)} slides)"}


def normalize_slides(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    slides = []
    for item in value:
        if isinstance(item, dict):
            title = str(item.get("title") or "Untitled")
            bullets = item.get("bullets") or []
            if not isinstance(bullets, list):
                bullets = [str(bullets)]
            slides.append({"title": title, "bullets": [str(bullet) for bullet in bullets]})
        else:
            slides.append({"title": str(item), "bullets": []})
    return slides


def content_types(count: int) -> str:
    slides = "\n".join(
        f'<Override PartName="/ppt/slides/slide{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        for i in range(1, count + 1)
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
<Override PartName="/ppt/presProps.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presProps+xml"/>
<Override PartName="/ppt/viewProps.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.viewProps+xml"/>
<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>
{slides}
</Types>"""


def root_rels() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""


def presentation_rels(count: int) -> str:
    slide_rels = "\n".join(
        f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide{i}.xml"/>'
        for i in range(1, count + 1)
    )
    theme_id = count + 1
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
{slide_rels}
<Relationship Id="rId{theme_id}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="theme/theme1.xml"/>
</Relationships>"""


def presentation_xml(count: int) -> str:
    slide_ids = "\n".join(
        f'<p:sldId id="{255 + i}" r:id="rId{i}"/>' for i in range(1, count + 1)
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
<p:sldMasterIdLst/>
<p:sldIdLst>{slide_ids}</p:sldIdLst>
<p:sldSz cx="12192000" cy="6858000" type="wide"/>
<p:notesSz cx="6858000" cy="9144000"/>
</p:presentation>"""


def slide_xml(index: int, slide: dict[str, Any]) -> str:
    title = escape_xml(str(slide.get("title") or "Untitled"))
    bullets = "".join(
        f'<a:p><a:pPr marL="342900" indent="-171450"/><a:r><a:rPr lang="ja-JP" sz="2800"/><a:t>{escape_xml(str(bullet))}</a:t></a:r></a:p>'
        for bullet in slide.get("bullets", [])
    )
    body = bullets or '<a:p><a:r><a:rPr lang="ja-JP" sz="2800"/><a:t></a:t></a:r></a:p>'
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
<p:cSld><p:spTree>
<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
<p:sp><p:nvSpPr><p:cNvPr id="2" name="Title {index}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
<p:spPr><a:xfrm><a:off x="685800" y="457200"/><a:ext cx="10820400" cy="914400"/></a:xfrm></p:spPr>
<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="ja-JP" sz="4400" b="1"/><a:t>{title}</a:t></a:r></a:p></p:txBody></p:sp>
<p:sp><p:nvSpPr><p:cNvPr id="3" name="Content {index}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
<p:spPr><a:xfrm><a:off x="914400" y="1600200"/><a:ext cx="10363200" cy="4572000"/></a:xfrm></p:spPr>
<p:txBody><a:bodyPr/><a:lstStyle/>{body}</p:txBody></p:sp>
</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sld>"""


def app_props(count: int) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
<Application>Little Agent</Application><PresentationFormat>On-screen Show (16:9)</PresentationFormat><Slides>{count}</Slides>
</Properties>"""


def core_props() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<dc:creator>Little Agent</dc:creator><cp:lastModifiedBy>Little Agent</cp:lastModifiedBy>
</cp:coreProperties>"""


def empty_part(root_name: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<{root_name} xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>"""


def theme_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Little Agent">
<a:themeElements><a:clrScheme name="Office"><a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1><a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1><a:dk2><a:srgbClr val="1F2937"/></a:dk2><a:lt2><a:srgbClr val="F8FAFC"/></a:lt2><a:accent1><a:srgbClr val="2563EB"/></a:accent1><a:accent2><a:srgbClr val="16A34A"/></a:accent2><a:accent3><a:srgbClr val="DC2626"/></a:accent3><a:accent4><a:srgbClr val="9333EA"/></a:accent4><a:accent5><a:srgbClr val="EA580C"/></a:accent5><a:accent6><a:srgbClr val="0891B2"/></a:accent6><a:hlink><a:srgbClr val="2563EB"/></a:hlink><a:folHlink><a:srgbClr val="9333EA"/></a:folHlink></a:clrScheme><a:fontScheme name="Office"><a:majorFont><a:latin typeface="Aptos Display"/></a:majorFont><a:minorFont><a:latin typeface="Aptos"/></a:minorFont></a:fontScheme><a:fmtScheme name="Office"><a:fillStyleLst/><a:lnStyleLst/><a:effectStyleLst/><a:bgFillStyleLst/></a:fmtScheme></a:themeElements>
</a:theme>"""


def safe_path(workspace: Path, requested: str, suffix: str) -> Path:
    path = Path(requested)
    if not path.is_absolute():
        path = workspace / path
    resolved = path.resolve()
    if workspace not in [resolved, *resolved.parents]:
        raise ValueError("Path escaped the workspace.")
    if resolved.suffix.lower() != suffix:
        raise ValueError(f"Expected a {suffix} file.")
    return resolved


def slide_number(path: str) -> int:
    match = re.search(r"slide(\d+)\.xml$", path)
    return int(match.group(1)) if match else 0


def escape_xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


if __name__ == "__main__":
    raise SystemExit(main())
