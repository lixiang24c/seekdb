#!/usr/bin/env python3
"""Check the published material-format constraints, not learning quality."""
import argparse
import json
import sys
from pathlib import Path


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"重复 JSON 键：{key!r}")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError(f"非标准 JSON 数值：{value}")


def string_pointers(value, path=""):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, child in value.items():
            escaped = key.replace("~", "~0").replace("/", "~1")
            yield from string_pointers(child, path + "/" + escaped)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from string_pointers(child, path + "/" + str(index))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default="agent/conversation.json")
    parser.add_argument("--pointers", action="store_true",
                        help="仅列出字符串字段位置和字符数，不打印内容")
    args = parser.parse_args()
    try:
        raw = Path(args.path).read_bytes()
        if len(raw) > 128 * 1024:
            raise ValueError(f"文件 {len(raw)} 字节，超过 128 KiB")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                           parse_constant=reject_constant)
        if not isinstance(value, dict):
            raise ValueError("顶层必须是对象")
    except (OSError, ValueError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(f"PASS: 格式检查通过，{len(raw)} / 131072 字节")
    print("本工具不评判真实性、证据去重、敏感信息或学习得分。")
    if value.get("synthetic") is True:
        print("注意：顶层 synthetic=true，合成材料不作为真人学习证据。")
    if args.pointers:
        for pointer, content in string_pointers(value):
            print(f"{pointer}\t{len(content)} 字符")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
