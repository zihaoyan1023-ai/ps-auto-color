#!/usr/bin/env python3
"""Read-only file checks; exit 0 is not visual or aesthetic approval."""
import sys
sys.dont_write_bytecode = True

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import xml.etree.ElementTree as ET

CRS = "{http://ns.adobe.com/camera-raw-settings/1.0/}"
RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"


class Incomplete(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def fingerprint(value):
    return (value.st_dev, value.st_ino, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


def crs_attributes(element):
    return {key[len(CRS):]: value for key, value in element.attrib.items()
            if key.startswith(CRS)}


def xml_node(element):
    return {"tag": element.tag, "attributes": dict(element.attrib),
            "text": (element.text or "").strip(),
            "children": [xml_node(child) for child in element]}


def inspect_xmp(stream):
    root = ET.parse(stream).getroot()
    descriptions = [node for node in root.iter(RDF + "Description")
                    if crs_attributes(node)]
    enhancement = []
    for node in root.iter():
        for key, value in node.attrib.items():
            if key.startswith(CRS) and re.search(
                    r"denoise|enhance|rawdetail|superresolution", key, re.I):
                enhancement.append({"element": node.tag, "field": key, "value": value})
        if node.tag.startswith(CRS) and re.search(
                r"denoise|enhance|rawdetail|superresolution", node.tag, re.I):
            enhancement.append({"element": xml_node(node)})
    present = any(node.tag.startswith(CRS) or crs_attributes(node) for node in root.iter())
    return {"xml_parse_ok": True, "camera_raw_metadata_present": present,
            "units": "raw XML values; no AI completion inference",
            "global": crs_attributes(descriptions[0]) if descriptions else {},
            "structures": [xml_node(child) for child in descriptions[0]
                           if child.tag.startswith(CRS)] if descriptions else [],
            "local_masks": [xml_node(node) for node in root.iter()
                            if node.get(CRS + "What") == "Correction"],
            "enhancement_fields": enhancement}


def inspect_jpeg(stream, result):
    from PIL import Image
    with Image.open(stream) as image:
        if image.format != "JPEG":
            raise ValueError("--jpeg input is not a JPEG")
        image.verify()
    result["verify_ok"] = True
    stream.seek(0)
    with Image.open(stream) as image:
        image.load()
        result.update(decode_ok=True, format=image.format, size=list(image.size),
                      mode=image.mode, bits_per_channel=getattr(image, "bits", None))
        profile = image.info.get("icc_profile")
    result["icc"] = {"present": bool(profile), "description": None}
    if profile:
        result["icc"].update(bytes=len(profile), sha256=hashlib.sha256(profile).hexdigest())
        from PIL import ImageCms
        result["icc"]["description"] = ImageCms.ImageCmsProfile(
            io.BytesIO(profile)).profile.profile_description
    if result["bits_per_channel"] is None:
        raise Incomplete("Pillow did not expose JPEG sample precision; not inferred from mode")


def inspect_file(kind, filename, expected=None):
    path = Path(filename).expanduser().absolute()
    result = {"path": str(path), "status": "error"}
    code = 0
    try:
        before = path.stat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("input is not a regular file")
        result.update(bytes=before.st_size, mtime_ns=before.st_mtime_ns)
        with path.open("rb") as stream:
            if fingerprint(before) != fingerprint(os.fstat(stream.fileno())):
                raise ValueError("file changed before reading")
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            result["sha256"] = digest.hexdigest()
            if before.st_size == 0:
                raise ValueError("input file is empty")
            if kind == "raw" and expected:
                result["expected_sha256"] = expected
                result["hash_matches"] = result["sha256"] == expected
                if not result["hash_matches"]:
                    raise ValueError("RAW SHA-256 does not match baseline")
            stream.seek(0)
            if kind == "xmp":
                result["xmp"] = inspect_xmp(stream)
            elif kind == "jpeg":
                result["jpeg"] = {}
                inspect_jpeg(stream, result["jpeg"])
            else:
                result["inspection"] = "file readability and hash only; no format decoding"
            if fingerprint(before) != fingerprint(os.fstat(stream.fileno())):
                raise ValueError("file changed during inspection")
        if fingerprint(before) != fingerprint(path.stat()):
            raise ValueError("file changed or was replaced during inspection")
        result.update(status="ok", unchanged_during_check=True)
    except (ImportError, Incomplete) as error:
        code = 2
        result.update(status="incomplete", error=str(error))
    except Exception as error:
        code = 1
        result["error"] = str(error)
    return result, code


def main(argv=None):
    parser = Parser(description=__doc__)
    for kind in ("raw", "xmp", "acr", "jpeg"):
        parser.add_argument("--" + kind)
    parser.add_argument("--expected-raw-sha256")
    try:
        args = parser.parse_args(argv)
        requested = {kind: getattr(args, kind) for kind in ("raw", "xmp", "acr", "jpeg")
                     if getattr(args, kind) is not None}
        if not requested:
            raise ValueError("request at least one of --raw, --xmp, --acr, --jpeg")
        expected = args.expected_raw_sha256
        if expected is not None and (not args.raw or not re.fullmatch(r"[0-9a-fA-F]{64}", expected)):
            raise ValueError("--expected-raw-sha256 requires --raw and exactly 64 hex characters")
        results, codes = {}, []
        for kind, filename in requested.items():
            result, code = inspect_file(kind, filename, expected.lower() if expected else None)
            results[kind] = result
            codes.append(code)
        code = 1 if 1 in codes else 2 if 2 in codes else 0
        output = {"status": {0: "ok", 1: "error", 2: "incomplete"}[code],
                  "scope": "independent requested file checks; no render linkage, aesthetic or AI completion approval",
                  "files": results}
    except ValueError as error:
        code, output = 1, {"status": "error", "error": str(error), "files": {}}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
