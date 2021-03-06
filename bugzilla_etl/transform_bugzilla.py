# encoding: utf-8
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Author: Kyle Lahnakoski (kyle@lahnakoski.com)
#

from __future__ import absolute_import
from __future__ import division
from __future__ import unicode_literals

import re
from datetime import date

from jx_python import jx
from mo_dots import listwrap
from mo_future import text_type, long
from mo_logs import Log
from mo_times import Date
from pyLibrary import convert
from pyLibrary.env import elasticsearch

DIFF_FIELDS = ["cf_user_story"]
LONG_FIELDS = ["short_desc"]
MULTI_FIELDS = ["cc", "blocked", "dependson", "dupe_by", "dupe_of", "keywords", "bug_group", "see_also"]
TIME_FIELDS = ["cf_due_date", "cf_last_resolved"]
NUMERIC_FIELDS=[
    "blocked",
    "dependson",
    "dupe_by",
    "dupe_of",
    "votes",
    "estimated_time",
    "remaining_time",
    "everconfirmed",
    "uncertain",
    "remaining_time"
]
ZERO_IS_NULL = ["votes", "remaining_time"]
NULL_VALUES = ['--', '---', '']

# Used to reformat incoming dates into the expected form.
# Example match: "2012/01/01 00:00:00.000"
DATE_PATTERN_STRICT = re.compile("^[0-9]{4}[\\/-][0-9]{2}[\\/-][0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}.[0-9]{3}")
DATE_PATTERN_STRICT_SHORT = re.compile("^[0-9]{4}[\\/-][0-9]{2}[\\/-][0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}")
# Example match: "2012-08-08 0:00"
DATE_PATTERN_RELAXED = re.compile("^[0-9]{4}[\\/-][0-9]{2}[\\/-][0-9]{2}")


#NORMALIZE BUG VERSION TO STANDARD FORM
def normalize(bug):
    bug=bug.copy()
    bug.id = text_type(bug.bug_id) + "_" + text_type(bug.modified_ts)[:-3]
    bug._id = None

    #ENSURE STRUCTURES ARE SORTED
    # Do some processing to make sure that diffing between runs stays as similar as possible.
    bug.flags=sort(bug.flags, "value")

    if bug.attachments:
        bug.attachments = sort(bug.attachments, "attach_id")
        for a in bug.attachments:
            for k,v in list(a.items()):
                if k.startswith("attachments") and (k.endswith("isobsolete") or k.endswith("ispatch") or k.endswith("isprivate")):
                    new_v = convert.value2int(v)
                    del a[k]
                    a[k[12:]] = new_v
                elif k.startswith("attachments") and k.endswith("mimetype"):
                    del a[k]
                    a[k[12:]] = v
            a.flags = sort(a.flags, ["modified_ts", "requestee", "value"])

    if bug.changes != None:
        for c in listwrap(bug.changes):
            c.new_value = sort(c.new_value)
            c.old_value = sort(c.old_value)
        bug.changes = sort(bug.changes, ["attach_id", "field_name"])

    for k, v in list(bug.items()):
        if v in NULL_VALUES:
            bug[k] = None

    for f in NUMERIC_FIELDS:
        v = bug[f]
        if v == None:
            continue
        elif f in MULTI_FIELDS:
            try:
                bug[f] = jx.sort(convert.value2intlist(v))
            except Exception as e:
                Log.error("not expected", cause=e)
        elif f in ZERO_IS_NULL and convert.value2number(v) == 0:
            del bug[f]
        else:
            bug[f]=convert.value2number(v)

    for f in MULTI_FIELDS:
        v = listwrap(bug[f])
        if v:
            bug[f] = jx.sort(v)

    # Also reformat some date fields
    for dateField in ["deadline", "cf_due_date", "cf_last_resolved"]:
        v = bug[dateField]
        if v == None:
            continue
        try:
            if isinstance(v, date):
                bug[dateField] = convert.datetime2milli(v)
            elif isinstance(v, (long, int, float)) and (text_type(v).endswith(('e+11', 'e+12')) or len(text_type(v)) in [12, 13]):
                bug[dateField] = v
            elif not isinstance(v, text_type):
                Log.error("situation not handled")
            elif DATE_PATTERN_STRICT.match(v):
                # Convert to "2012/01/01 00:00:00.000"
                # Example: bug 856732 (cf_last_resolved)
                # dateString = v.substring(0, 10).replace("/", '-') + "T" + v.substring(11) + "Z"
                bug[dateField] = convert.datetime2milli(convert.string2datetime(v+"000", "%Y/%m/%d %H:%M%:S%f"))
            elif DATE_PATTERN_STRICT_SHORT.match(v):
                # Convert "2012/01/01 00:00:00" to "2012-01-01T00:00:00.000Z", then to a timestamp.
                # Example: bug 856732 (cf_last_resolved)
                # dateString = v.substring(0, 10).replace("/", '-') + "T" + v.substring(11) + "Z"
                bug[dateField] = convert.datetime2milli(convert.string2datetime(v.replace("-", "/"), "%Y/%m/%d %H:%M:%S"))
            elif DATE_PATTERN_RELAXED.match(v):
                # Convert "2012/01/01 00:00:00.000" to "2012-01-01"
                # Example: bug 643420 (deadline)
                #          bug 726635 (cf_due_date)
                bug[dateField] = convert.datetime2milli(convert.string2datetime(v[0:10], "%Y-%m-%d"))
        except Exception as e:
            Log.error("problem with converting date to milli (type={{type}}, value={{value}})", value=v, type=type(v), cause=e)

    bug.votes = None
    bug.etl.timestamp = Date.now()

    bug = elasticsearch.scrub(bug)
    return bug


def sort(value, param=None):
    return jx.sort(listwrap(value), param)
