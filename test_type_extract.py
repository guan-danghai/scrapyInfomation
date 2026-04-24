#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试：从标题推断 info_type"""

import re
import sys
import json

sys.path.insert(0, __import__('os').path.dirname(__file__))


def extract_type_from_title(raw_title: str):
    clean = re.sub(r"^\[[^\[\]]{1,20}\]", "", raw_title).strip() or raw_title.strip()
    keywords = [
        ("\u6d41\u6807",          "\u6d41\u6807"),
        ("\u6210\u4ea4\u7ed3\u679c",        "\u6210\u4ea4\u7ed3\u679c"),
        ("\u4e2d\u6807\u5019\u9009\u4eba\u516c\u793a",   "\u4e2d\u6807\u5019\u9009\u4eba\u516c\u793a"),
        ("\u5019\u9009\u4eba\u516c\u793a",      "\u4e2d\u6807\u5019\u9009\u4eba\u516c\u793a"),
        ("\u4e2d\u6807\u516c\u793a",        "\u4e2d\u6807\u516c\u793a"),
        ("\u8bc4\u6807\u7ed3\u679c\u516c\u793a",     "\u8bc4\u6807\u7ed3\u679c\u516c\u793a"),
        ("\u4e2d\u6807\u7ed3\u679c\u516c\u793a",     "\u4e2d\u6807\u7ed3\u679c\u516c\u793a"),
        ("\u4e2d\u6807\u7ed3\u679c",        "\u4e2d\u6807\u7ed3\u679c"),
        ("\u4e2d\u6807\u516c\u544a",        "\u4e2d\u6807\u516c\u544a"),
        ("\u4e2d\u6807",           "\u4e2d\u6807"),
        ("\u7ade\u4e89\u6027\u78cb\u5546",      "\u7ade\u4e89\u6027\u78cb\u5546"),
        ("\u78cb\u5546",           "\u78cb\u5546"),
        ("\u7ade\u4e89\u6027\u8c08\u5224",      "\u7ade\u4e89\u6027\u8c08\u5224"),
        ("\u8c08\u5224",           "\u8c08\u5224"),
        ("\u8be2\u4ef7",           "\u8be2\u4ef7"),
        ("\u9080\u8bf7\u62db\u6807",        "\u9080\u8bf7\u62db\u6807"),
        ("\u62db\u6807\u516c\u544a",        "\u62db\u6807\u516c\u544a"),
        ("\u62db\u6807",           "\u62db\u6807"),
        ("\u516c\u793a",           "\u516c\u793a"),
        ("\u5f81\u96c6",           "\u5f81\u96c6"),
        ("\u9080\u8bf7",           "\u9080\u8bf7"),
        ("\u91c7\u8d2d\u516c\u544a",        "\u91c7\u8d2d\u516c\u544a"),
        ("\u91c7\u8d2d",           "\u91c7\u8d2d"),
    ]
    for kw, label in keywords:
        if kw in clean:
            return label, clean
    return "\u91c7\u62db\u4fe1\u606f", clean


CASES = [
    (
        "\u4e2d\u56fd\u957f\u57ce\u8d44\u4ea7\u7ba1\u7406\u80a1\u4efd\u6709\u9650\u516c\u53f82026\u5e74\u8d77\u4e24\u5e74\u684c\u9762\u8fd0\u7ef4\u5916\u5305\u670d\u52a1\u91c7\u8d2d\u9879\u76ee\u8bc4\u6807\u7ed3\u679c\u516c\u793a",
        "\u8bc4\u6807\u7ed3\u679c\u516c\u793a"
    ),
    (
        "[\u5df2\u6d41\u6807]\u91d1\u534e\u9280\u884c\u5f02\u5730\u4e92\u8054\u7f51\u6570\u636e\u4e2d\u5fc3\uff08IDC\uff09\u4e1a\u52a1\u9879\u76ee\uff08\u91cd\u65b0\u62db\u6807\uff09\u6d41\u6807\u516c\u544a",
        "\u6d41\u6807"
    ),
    (
        "[\u9633\u5149\u91c7\u8d2d\u670d\u52a1\u5e73\u53f0]\u5b81\u6ce2\u9280\u884c\u57fa\u5efa\u77f3\u6750\u4f9b\u5e94\u5546\u5165\u56f4\u9879\u76ee\u8d44\u683c\u9884\u5ba1\u8bc4\u5ba1\u516c\u793a",
        "\u516c\u793a"
    ),
    (
        "\u4e2d\u56fd\u5149\u5927\u9280\u884c\u751f\u4ea7\u73af\u5883\u534e\u4e3aSAN\u5b58\u50a8\u8bbe\u5907\u7ef4\u4fdd\u670d\u52a1\u91c7\u8d2d\u9879\u76ee\u8be2\u4ef7\u516c\u544a",
        "\u8be2\u4ef7"
    ),
    (
        "\u4e2d\u56fd\u9280\u884c\u6f4d\u574a\u5206\u884c\u9009\u53d6\u4e2d\u5fc3\u673a\u623f\u5024\u5b88\u670d\u52a1\u9879\u76ee\uff08\u4e8c\u6b21\uff09\u7ade\u4e89\u6027\u78cb\u5546\u516c\u544a",
        "\u7ade\u4e89\u6027\u78cb\u5546"
    ),
]

results = []
all_pass = True
for raw, expected in CASES:
    info_type, clean = extract_type_from_title(raw)
    passed = info_type == expected
    if not passed:
        all_pass = False
    results.append({
        "raw": raw,
        "info_type": info_type,
        "expected": expected,
        "pass": passed,
        "clean_title": clean,
    })

with open(r"e:\caiwu\caizhaowang\test_type_result.json", "w", encoding="utf-8") as f:
    json.dump({"all_pass": all_pass, "cases": results}, f, ensure_ascii=False, indent=2)

print("done, all_pass =", all_pass)
