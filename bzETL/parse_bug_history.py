################################################################################
## This Source Code Form is subject to the terms of the Mozilla Public
## License, v. 2.0. If a copy of the MPL was not distributed with this file,
## You can obtain one at http://mozilla.org/MPL/2.0/.
################################################################################
## PYTHON VERISON OF ../resources/javascript/parse_bug_history.js
################################################################################


# vim: set filetype=javascript ts=2 et sw=2: */
# Workflow:
# Create the current state object
#
# for row containing latest state data (fields from bugs table record, fields from other tables (i.e. attachments, dependencies)
# Update the current state object with the latest field values
#
# Walk backward through activity records from bugs_activity (and other activity type tables). for set of activities:
# Create a new bug version object with the meta data about this activity
# Set id based on modification time
# *       Set valid_from field as modification time
# *       Set valid_to field as the modification time of the later version - 1 second
# Add modification data (who, when, what)
# For single value fields (i.e. assigned_to, status):
# Update the original state object by replacing the field value with the contents of the activities "removed" column
# For multi-value fields (i.e. blocks, CC, attachments):
# If a deletion, update the original state object by adding the value from the "removed" column to the field values array.
# If an addition, find and remove the added item from the original state object
#
# When finished with all activities, the current state object should reflect the original state of the bug when created.
# Now, build the full state of each intermediate version of the bug.
#
# for bug version object that was created above:
# Merge the current state object into this version object
# Update fields according to the modification data
#
# When doing an incremental update (ie. with START_TIME specified), Look at any bug that has been modified since the
# cutoff time, and build all versions.  Only index versions after START_TIME in ElasticSearch.


# Used to split a flag into (type, status [,requestee])
# Example: "review?(mreid@mozilla.com)" -> (review, ?, mreid@mozilla.com)
# Example: "review-" -> (review, -)
import re
import math
from bzETL.util import struct
from transform_bugzilla import normalize, NUMERIC_FIELDS, MULTI_FIELDS

from bzETL.util.cnv import CNV
from bzETL.util.debug import D
from bzETL.util.query import Q
from bzETL.util.struct import Struct
from bzETL.util.files import File
from bzETL.util.maths import Math


FLAG_PATTERN = re.compile("^(.*)([?+-])(\\([^)]*\\))?$")



# Fields that could have been truncated per bug 55161
TRUNC_FIELDS = ["cc", "blocked", "dependson", "keywords"]



class parse_bug_history_():

    def __init__(self, settings, output_queue):
        self.bzAliases = None
        self.startNewBug(struct.wrap({"bug_id":0, "modified_ts":0, "_merge_order":1}))
        self.prevActivityID = None
        self.prev_row=None
        self.settings=settings
        self.output = output_queue

        self.initializeAliases()
        

    def processRow(self, row_in):
        if len(row_in.items())==0: return 
        try:
            self.currBugID = row_in.bug_id
            if self.currBugState.cc is None:
                D.println("PROBLEM expecting a created_ts")

            if self.settings.debug: D.println("process row: {{row}}", {"row":row_in})

            # For debugging purposes:
            if self.settings.END_TIME > 0 and row_in.modified_ts > self.settings.END_TIME:
                D.println("Skipping change after END_TIME (" + self.settings.END_TIME + ")")
                return

            # If we have switched to a new bug
            if self.prevBugID < self.currBugID:
                if self.prevBugID>0:
                    # Start replaying versions in ascending order to build full data on each version
                    D.println("Emitting intermediate versions for {{bug_id}}", {"bug_id":self.prevBugID})
                    self.populateIntermediateVersionObjects()
                self.startNewBug(row_in)

            # Bugzilla bug workaround - some values were truncated, introducing uncertainty / errors:
            # https://bugzilla.mozilla.org/show_bug.cgi?id=55161
            if row_in.field_name in TRUNC_FIELDS:
                added=CNV.value2string(row_in.new_value)
                removed=CNV.value2string(row_in.old_value)
                uncertain = False


                if added in ["? ?", "?"]: # Unknown value extracted from a possibly truncated field
                    uncertain = True
                    D.println("PROBLEM Encountered uncertain added value.  Skipping.")
                    row_in.new_value = None
                elif added is not None and added.startswith("? "): # Possibly truncated value extracted from a possibly truncated field
                    uncertain = True
                    row_in.new_value = added[2:]

                if removed in ["? ?", "?"]:# Unknown value extracted from a possibly truncated field
                    uncertain = True
                    D.println("PROBLEM Encountered uncertain removed value.  Skipping.")
                    row_in.old_value = None
                elif removed is not None and removed.startswith("? "): # Possibly truncated value extracted from a possibly truncated field
                    uncertain = True
                    row_in.old_value = removed[2:]

                if uncertain and self.currBugState.uncertain is None:
                    # Process the "uncertain" flag as an activity
                    # WE ARE GOING BACKWARDS IN TIME, SO MARKUP PAST
                    D.println("PROBLEM Setting this bug to be uncertain.")
                    self.processBugsActivitiesTableItem(struct.wrap({
                        "modified_ts": row_in.modified_ts,
                        "modified_by": row_in.modified_by,
                        "field_name":"uncertain",
                        "new_value":None,
                        "old_value":"1",
                        "attach_id":None
                    }))
                    if row_in.new_value is None and row_in.old_value is None:
                        D.println("Nothing added or removed. Skipping update.")
                        return
            if self.currBugID < 999999999:
                # Treat timestamps as int values
                new_value = int(row_in.new_value) if row_in.field_name.endswith("_ts") else row_in.new_value
                if row_in.field_name=="bug_file_loc" and (row_in.new_value is None or len(row_in.new_value)>0):
                    D.println("bug_file_loc is empty")
                # Determine where we are in the bug processing workflow
                if row_in._merge_order==1:
                    self.processSingleValueTableItem(row_in.field_name, new_value)
                elif row_in._merge_order==2:
                    self.processMultiValueTableItem(row_in.field_name, new_value)
                elif row_in._merge_order==7:
                    self.processAttachmentsTableItem(row_in)
                elif row_in._merge_order==8:
                    self.processFlagsTableItem(row_in)
                elif row_in._merge_order==9:
                    self.processBugsActivitiesTableItem(row_in)
                else:
                    D.warning("Unhandled merge_order: '" + row_in._merge_order + "'")

        except Exception, e:
            D.warning("Problem processing row: {{row}}", {"row":row_in}, e)
        finally:
            for b in self.currBugState.blocked:
                if isinstance(b, basestring):
                    D.println("PROBLEM error")
            self.prev_row=row_in

    @staticmethod
    def uid(bug_id, modified_ts):
        return unicode(bug_id) + "_" + unicode(modified_ts)[0:-3]

    def startNewBug(self, row_in):
        self.prevBugID = row_in.bug_id
        self.bugVersions = []
        self.bugVersionsMap = Struct()
        self.currActivity = Struct()
        self.currBugAttachmentsMap = Struct()
        self.currBugState = Struct(
            _id=parse_bug_history_.uid(row_in.bug_id, row_in.modified_ts),
            bug_id=row_in.bug_id,
            modified_ts=row_in.modified_ts,
            modified_by=row_in.modified_by,
            reported_by=row_in.modified_by,
            attachments=[]
        )
        #WE FORCE ADD ALL SETS, AND WE WILL scrub() THEM OUT LATER IF NOT USED
        for f in MULTI_FIELDS:
            self.currBugState[f]=set([])
        self.currBugState.flags=[]   #FLAGS ARE MULTI_FIELDS, BUT ARE ALSO STRUCTS, SO MUST BE IN AN ARRAY


        if row_in._merge_order != 1:
            # Problem: No entry found in the 'bugs' table.
            D.warning("Current bugs table record not found for bug_id: {{bug_id}}  (merge order should have been 1, but was {{start_time}})", row_in)


    def processSingleValueTableItem(self, field_name, new_value):
        self.currBugState[field_name] = new_value

    def processMultiValueTableItem(self, field_name, new_value):
        if field_name in NUMERIC_FIELDS: new_value=int(new_value)
        try:
            self.currBugState[field_name].add(new_value)
            return None
        except Exception, e:
            D.warning("Unable to push {{value}} to array field {{start_time}} on bug {{curr_value}} current value: {{curr_value}}",{
                "value":new_value,
                "field":field_name,
                "bug_id":self.currBugID,
                "curr_value":self.currBugState[field_name]
            }, e)


    def processAttachmentsTableItem(self, row_in):
        if row_in.attach_id==349397:
            D.println("")

        currActivityID = parse_bug_history_.uid(self.currBugID, row_in.modified_ts)
        if currActivityID != self.prevActivityID:
            self.prevActivityID = currActivityID

            self.currActivity =Struct(
                _id=currActivityID,
                modified_ts=row_in.modified_ts,
                modified_by= row_in.modified_by,
                changes= [{
                    "field_name":"attachment_added",
                    "attach_id":row_in.attach_id
                }]
            )

            self.bugVersions.append(self.currActivity)
            self.bugVersionsMap[currActivityID] = self.currActivity


        att=self.currBugAttachmentsMap[unicode(row_in.attach_id)]
        if att is None:
            att={
                "attach_id": row_in.attach_id,
                "modified_ts": row_in.modified_ts,
                "created_ts": row_in.created_ts,
                "modified_by": row_in.modified_by,
                "flags": []
            }
            self.currBugAttachmentsMap[unicode(row_in.attach_id)]=att

        att["created_ts"]=Math.min(row_in.modified_ts, att["created_ts"])
        if row_in.field_name=="created_ts" and row_in.new_value is None:
            pass
        else:
            att[row_in.field_name] = row_in.new_value

            
    def processFlagsTableItem(self, row_in):
        flag = self.makeFlag(row_in.new_value, row_in.modified_ts, row_in.modified_by)
        if row_in.attach_id is not None:
            if self.currBugAttachmentsMap[unicode(row_in.attach_id)] is None:
                D.println("PROBLEM Unable to find attachment {{attach_id}} for bug_id {{start_time}}", {
                    "attach_id":row_in.attach_id,
                    "bug_id":self.currBugID
                })

            self.currBugAttachmentsMap[unicode(row_in.attach_id)].flags.append(flag)
        else:
            self.currBugState.flags.append(flag)


    def processBugsActivitiesTableItem(self, row_in):
        if self.currBugState.created_ts is None:
            D.error("must have created_ts")

        if row_in.field_name == "flagtypes_name":
            row_in.field_name = "flags"

        multi_field_value = self.getMultiFieldValue(row_in.field_name, row_in.new_value)
        multi_field_value_removed = parse_bug_history_.getMultiFieldValue(row_in.field_name, row_in.old_value)

        currActivityID = parse_bug_history_.uid(self.currBugID, row_in.modified_ts)
        if currActivityID != self.prevActivityID:
            self.currActivity = self.bugVersionsMap[currActivityID]
            if self.currActivity is None:
                self.currActivity = Struct(
                    _id= currActivityID,
                    modified_ts= row_in.modified_ts,
                    modified_by= row_in.modified_by,
                    changes= []
                )
                self.bugVersions.append(self.currActivity)

            self.prevActivityID = currActivityID


        if row_in.attach_id is not None:
            attachment = self.currBugAttachmentsMap[unicode(row_in.attach_id)]
            if attachment is None:
                #we are going backwards in time, no need to worry about these?  maybe delete this change for public bugs
                D.println("PROBLEM Unable to find attachment {{attach_id}} for bug_id {{start_time}}: {{start_time}}", {
                    "attach_id":row_in.attach_id,
                    "bug_id":self.currBugID,
                    "attachments":self.currBugAttachmentsMap
                })
                self.currActivity.changes.append({
                    "field_name": row_in.field_name,
                    "new_value": row_in.new_value,
                    "old_value": row_in.old_value,
                    "attach_id": row_in.attach_id
                })
            else:

                if row_in.field_name in MULTI_FIELDS:
                    a = attachment[row_in.field_name]
                    # Can have both added and removed values.
                    a=self.removeValues(a, multi_field_value, "added", row_in.field_name, "attachment", attachment)
                    a=self.addValues(a, multi_field_value_removed, "removed attachment", row_in.field_name, attachment)
                    attachment[row_in.field_name]=a
                else:
                    attachment[row_in.field_name] = row_in.old_value
                    self.currActivity.changes.append({
                        "field_name": row_in.field_name,
                        "new_value": row_in.new_value,
                        "old_value": row_in.old_value,
                        "attach_id": row_in.attach_id
                    })

        else:
            if row_in.field_name in MULTI_FIELDS:
                # PROBLEM: WHEN GOING BACK IN HISTORY, AND THE ADDED VALUE IS NOT FOUND IN THE CURRENT
                # STATE, IT IS STILL RECORDED (see above self.currActivity.changes.append...).  THIS MEANS
                # WHEN GOING THROUGH THE CHANGES IN IN ORDER THE VALUE WILL EXIST, BUT IT SHOULD NOT
                a = self.currBugState[row_in.field_name]
                # Can have both added and removed values.
                a = self.removeValues(a, multi_field_value, "added", row_in.field_name, "currBugState", self.currBugState)
                a = self.addValues(a, multi_field_value_removed, "removed bug", row_in.field_name, self.currBugState)
                self.currBugState[row_in.field_name]=a
                if a is None:
                    D.println("PROBLEM error")
            else:
                # Replace current value
                self.currBugState[row_in.field_name] = row_in.old_value
                self.currActivity.changes.append({
                    "field_name": row_in.field_name,
                    "new_value": row_in.new_value,
                    "old_value": row_in.old_value,
                    "attach_id": row_in.attach_id
                })

    @staticmethod
    def sortAscByField(a, b, aField):
        if a[aField] > b[aField]:
            return 1
        if a[aField] < b[aField]:
            return -1
        return 0

    @staticmethod
    def sortDescByField(a, b, aField):
        return -1 * parse_bug_history_.sortAscByField(a, b, aField)

    
    def populateIntermediateVersionObjects(self):
        # Make sure the self.bugVersions are in descending order by modification time.
        # They could be mixed because of attachment activity
        self.bugVersions=Q.sort(self.bugVersions, [
                {"field":"modified_ts", "sort":-1}
        ])

        # Tracks the previous distinct value for field
        prevValues ={}
        currVersion=None
        # Prime the while loop with an empty next version so our first iteration outputs the initial bug state
        nextVersion = Struct(_id=self.currBugState._id, changes=[])

        flagMap ={}
        # A monotonically increasing version number (useful for debugging)
        self.currBugVersion = 1

        # continue if there are more bug versions, or there is one final nextVersion
        while len(self.bugVersions) > 0 or nextVersion is not None:
            try:
                currVersion = nextVersion
                if len(self.bugVersions) > 0:
                    nextVersion = self.bugVersions.pop() # Oldest version
                else:
                    nextVersion = None

                D.println("Populating JSON for version {{id}}", {"id":currVersion._id})
                # Decide whether to merge this bug activity into the current state (without emitting
                # a separate JSON document). This addresses the case where an attachment is created
                # at exactly the same time as the bug itself.
                # Effectively, we combine all the changes for a given timestamp into the last one.
                mergeBugVersion = False
                if nextVersion is not None and currVersion._id == nextVersion._id:
                    D.println("Merge mode: activated " + self.currBugState._id)
                    mergeBugVersion = True

                # Link this version to the next one (if there is a next one)
                if nextVersion is not None:
                    D.println("We have a nextVersion: {{timestamp}} (ver {{next_version}})", {
                        "timestamp":nextVersion.modified_ts,
                        "next_version":self.currBugVersion + 1
                    })
                    self.currBugState.expires_on = nextVersion.modified_ts
                else:
                    # Otherwise, we don't know when the version expires.
                    D.println("We have no nextVersion after #{{version}}", {"version": self.currBugVersion})

                    self.currBugState.expires_on = None

                # Copy all attributes from the current version into self.currBugState
                for propName, propValue in currVersion.items():
                    self.currBugState[propName] = propValue

                # Now walk self.currBugState forward in time by applying the changes from currVersion
                #BE SURE TO APPLY REMOVES BEFORE ADDS, JUST IN CASE BOTH HAPPENED TO ONE FIELD
                changes = Q.sort(currVersion.changes, ["attach_id", "field_name", {"field":"old_value", "sort":-1}, "new_value"])
                currVersion.changes = changes
                self.currBugState.changes = changes

                for c, change in enumerate(changes):
                    if c + 1 < len(changes):
                        #PACK ADDS AND REMOVES TO SINGLE CHANGE TO MATCH ORIGINAL
                        next = changes[c + 1]
                        if change.attach_id == next.attach_id and\
                           change.field_name == next.field_name and\
                           change.old_value is not None and\
                           next.old_value is None:
                            next.old_value = change.old_value
                            changes[c] = None
                            continue
                        if change.new_value is None and \
                           change.old_value is None and \
                           change.field_name!="attachment_added":
                            changes[c] = None
                            continue

                    D.println("Processing change: " + CNV.object2JSON(change))
                    target = self.currBugState
                    targetName = "currBugState"
                    attach_id = change.attach_id
                    if attach_id is not None:



                        # Handle the special change record that signals the creation of the attachment
                        if change.field_name == "attachment_added":
                            if attach_id==349397:
                                D.println("")
                            # This change only exists when the attachment has been added to the map, so no missing case needed.
                            att=self.currBugAttachmentsMap[unicode(attach_id)]
                            self.currBugState.attachments.append(att)
                            continue
                        else:
                            # Attachment change
                            target = self.currBugAttachmentsMap[unicode(attach_id)]
                            targetName = "attachment"
                            if target is None:
                                D.warning("Encountered a change to missing attachment for bug '"
                                    + currVersion["bug_id"] + "': " + CNV.object2JSON(change) + ".")

                                # treat it as a change to the main bug instead :(
                                target = self.currBugState
                                targetName = "currBugState"



                    if change.field_name == "flags":
                        self.processFlagChange(target, change, currVersion.modified_ts, currVersion.modified_by)
                    elif change.field_name not in MULTI_FIELDS:
                        # Track the previous value
                        # Single-value field has changed in bug or attachment
                        # Make sure it's actually changing.  We seem to get change
                        # entries for attachments that show the current field value.
                        if target[change.field_name] != change.new_value:
                            self.setPrevious(target, change.field_name, target[change.field_name], currVersion.modified_ts)
                        else:
                            D.println("PROBLEM Skipping fake change to " + targetName + ": "
                                + CNV.object2JSON(target) + ", change: " + CNV.object2JSON(change))

                    else:
                        D.println("Skipping previous_value for " + targetName
                            + " multi-value field " + change.field_name)

                    # Multi-value fields
                    if change.field_name == "flags":
                        # Already handled by "processFlagChange" above.
                        D.println("Skipping previously processed flag change")
                    elif change.field_name in MULTI_FIELDS:
                        a = target[change.field_name]
                        multi_field_value = parse_bug_history_.getMultiFieldValue(change.field_name, change.new_value)
                        multi_field_value_removed = parse_bug_history_.getMultiFieldValue(change.field_name,
                                                                                          change.old_value)

                        # This was a deletion, find and delete the value(s)
                        a = self.removeValues(a, multi_field_value_removed, "removed", change.field_name, targetName, target)
                        # Handle addition(s) (if any)
                        a = self.addValues(a, multi_field_value, "added", change.field_name, target)
                        target[change.field_name]=a
                    else:
                        # Simple field change.
                        target[change.field_name] = change.new_value


                self.currBugState.bug_version_num = self.currBugVersion

                if not mergeBugVersion:
                    # This is not a "merge", so output a row for this bug version.
                    self.currBugVersion+=1
                    # Output this version if either it was modified after START_TIME, or if it
                    # expired after START_TIME (the latter will update the last known version of the bug
                    # that did not have a value for "expires_on").
                    if self.currBugState.modified_ts >= self.settings.START_TIME or self.currBugState.expires_on >= self.settings.START_TIME:
                        state=normalize(self.currBugState)
                        if state.blocked is not None and len(state.blocked)==1 and "None" in state.blocked:
                            D.println("PROBLEM error")
                        D.println("Bug {{bug_state.bug_id}} v{{bug_state.bug_version_num}} (_id = {{bug_state._id}}): {{bug_state}}" , {
                            "bug_state":state
                        })
                        self.output.add(state)

                    else:
                        D.println("PROBLEM Not outputting ${-id} - it is before self.START_TIME ({{start_time}})", {
                            "_id":self.currBugState._id,
                            "start_time":self.settings.START_TIME
                        })

                else:
                    D.println("Merging a change with the same timestamp = {{bug_state._id}}: {{bug_state}}",{
                        "bug_state":currVersion
                    })
            finally:
                if self.currBugState.blocked is None:
                    D.println("expecting a created_ts")
                pass
            
    def findFlag(self, aFlagList, aFlag):
        existingFlag = self.findByKey(aFlagList, "value", aFlag.value)  # len([f for f in aFlagList if f.value==aFlag.value])>0   aFlag.value in Q.select(aFlagList, "value")
        if existingFlag is not None:
            return existingFlag

        for eFlag in aFlagList:
            if (
                eFlag.request_type == aFlag.request_type and
                eFlag.request_status == aFlag.request_status and
                aFlag.requestee is not None and
                eFlag.requestee is not None and
                (
                    aFlag.requestee.lower() + "=" + eFlag.requestee.lower() in self.bzAliases or # Try both directions.
                    eFlag.requestee.lower() + "=" + aFlag.requestee.lower() in self.bzAliases
                )
            ):
                D.println("Using bzAliases to match change '" + aFlag.value + "' to '" + eFlag.value + "'")
                return eFlag

            
    def processFlagChange(self, target, change, modified_ts, modified_by, reverse=False):
        addedFlags = parse_bug_history_.getMultiFieldValue("flags", change.new_value)
        removedFlags = parse_bug_history_.getMultiFieldValue("flags", change.old_value)

        #going in reverse when traveling through bugs backwards in time
        if reverse:
            (addedFlags, removedFlags)=(removedFlags, addedFlags)

        # First, mark any removed flags as straight-up deletions.
        for flagStr in removedFlags:
            if flagStr == "":
                continue

            flag = parse_bug_history_.makeFlag(flagStr, modified_ts, modified_by)
            existingFlag = self.findFlag(target.flags, flag)

            if existingFlag is not None:
                # Carry forward some previous values:
                existingFlag["previous_modified_ts"] = existingFlag["modified_ts"]
                if existingFlag["modified_by"] != modified_by:
                    existingFlag["previous_modified_by"] = existingFlag["modified_by"]
                    existingFlag["modified_by"] = modified_by

                # Add changed stuff:
                existingFlag["modified_ts"] = modified_ts
                existingFlag["previous_status"] = flag["request_status"]
                existingFlag["previous_value"] = flagStr
                existingFlag["request_status"] = "D"
                existingFlag["value"] = ""
                # request_type stays the same.
                # requestee stays the same.

                duration_ms = existingFlag["modified_ts"] - existingFlag["previous_modified_ts"]
                existingFlag["duration_days"] = math.floor(duration_ms / (1000.0 * 60 * 60 * 24))
            else:
                D.warning("Did not find a corresponding flag for removed value {{removed}} in {{existing}}",{
                    "removed":flagStr,
                    "existing":target.flags
                })

        # See if we can align any of the added flags with previous deletions.
        # If so, try to match them up with a "dangling" removed flag
        for flagStr in addedFlags:
            if flagStr == "":
                continue

            flag = self.makeFlag(flagStr, modified_ts, modified_by)

            if target.flags is None:
                D.println("PROBLEM  processFlagChange called with unset 'flags'")
                target.flags = []

            candidates = [element for element in target.flags if
                element["value"] == ""
                    and flag["request_type"] == element["request_type"]
                    and flag["request_status"] != element["previous_status"] # Skip "r?(dre@mozilla)" -> "r?(mark@mozilla)"
            ]

            if len(candidates) > 0:
                chosen_one = candidates[0]
                if len(candidates) > 1:
                    # Multiple matches - use the best one.
                    D.println("Matched added flag {{flag}} to multiple removed flags.  Using the best of these:\n", {
                        "flag":flag,
                        "candidates":candidates
                    })
                    matched_ts = [element for element in candidates if
                        flag.modified_ts == element.modified_ts
                    ]

                    if len(matched_ts) == 1:
                        D.println("Matching on modified_ts fixed it")
                        chosen_one = matched_ts[0]
                    else:
                        D.println("Matching on modified_ts left us with {{num}} matches", {"num":len(matched_ts)})
                        # If we had no matches (or many matches), try matching on requestee.
                        matched_req = [element for element in candidates if
                            # Do case-insenitive comparison
                            element["requestee"] is not None and
                                flag["modified_by"].lower() == element["requestee"].lower()
                        ]
                        if len(matched_req) == 1:
                            D.println("Matching on requestee fixed it")
                            chosen_one = matched_req[0]
                        else:
                            D.warning("Matching on requestee left us with {{num}} matches. Skipping match.", {"num":len(matched_req)})
                            # TODO: add "uncertain" flag?
                            chosen_one = None


                else:
                    # Obvious case - matched exactly one.
                    D.println("Matched added flag " + CNV.object2JSON(flag) + " to removed flag " + CNV.object2JSON(chosen_one))

                if chosen_one is not None:
                    for f in ["value", "request_status", "requestee"]:
                        if flag[f] is not None:
                            chosen_one[f] = flag[f]



                # We need to avoid later adding this flag twice, since we rolled an add into a delete.
            else:
                # No matching candidate. Totally new flag.
                D.println("PROBLEM Did not match added flag " + CNV.object2JSON(flag) + " to anything: " + CNV.object2JSON(target.flags))
                target.flags.append(flag)




    def setPrevious(self, dest, aFieldName, aValue, aChangeAway):
        if dest["previous_values"] is None:
            dest["previous_values"] ={}

        pv = dest["previous_values"]
        vField = aFieldName + "_value"
        caField = aFieldName + "_change_away_ts"
        ctField = aFieldName + "_change_to_ts"
        ddField = aFieldName + "_duration_days"

        pv[vField] = aValue
        # If we have a previous change for this field, then use the
        # change-away time as the new change-to time.
        if pv[caField] is not None:
            pv[ctField] = pv[caField]
        else:
            # Otherwise, this is the first change for this field, so
            # use the creation timestamp.
            pv[ctField] = dest["created_ts"]

        pv[caField] = aChangeAway
        try:
            duration_ms = pv[caField] - pv[ctField]
        except Exception, e:
            D.error("", e)
        pv[ddField] = math.floor(duration_ms / (1000.0 * 60 * 60 * 24))

    @staticmethod
    def findByKey(aList, aField, aValue):
        for item in aList:
            if isinstance(item, basestring):
                D.error("expecting structure")
            if item[aField] == aValue:
                return item
        return None



    @staticmethod
    def makeFlag(flag, modified_ts, modified_by):
        flagParts = Struct(
            modified_ts=modified_ts,
            modified_by=modified_by,
            value=flag
        )

        matches = FLAG_PATTERN.match(flag)
        if matches:
            flagParts.request_type = matches.group(1)
            flagParts.request_status = matches.group(2)
            if matches.start(3)!=-1 and len(matches.group(3)) > 2:
                flagParts.requestee = matches.group(3)[1:-1]


        return flagParts


    def addValues(self, total, add, valueType, field_name, target):
        if len(add)==0: return total
#        D.println("Adding " + valueType + " " + fieldName + " values:" + CNV.object2JSON(someValues))
        if field_name == "flags":
            for v in add:
                total.append(parse_bug_history_.makeFlag(v, target.modified_ts, target.modified_by))
            if valueType!="added":
                self.currActivity.changes.append({
                    "field_name": field_name,
                    "new_value": None,
                    "old_value": ", ".join(Q.sort(add)),
                    "attach_id": target.attach_id
                })
            return total
            ## TODO: Some bugs (like 685605) actually have duplicate flags. Do we want to keep them?
            #/*
            # # Check if this flag has already been incorporated into a removed flag. If so, don't add it again.
            # dupes = anArray.filter(def(element, index, array):
            # return element["value"] == added
            # and element["modified_by"] == anObj.modified_by
            # and element["modified_ts"] == anObj.modified_ts
            # })
            # if dupes and dupes.length > 0:
            # D.println("Skipping duplicated added flag '" + added + "' since info is already in " + CNV.object2JSON(dupes[0]))
            # else:
            # */
        else:
            diff=add-total
            removed=total&add

            #WE CAN NOT REMOVE VALUES WE KNOW TO BE THERE AFTER
            if len(removed)>0:
                D.println("PROBLEM: Found {{type}} {{field_name}} value: (Removing {{removed}} can not result in {{existing}})",{
                    "type":valueType,
                    "field_name":field_name,
                    "removed":removed,
                    "existing":target[field_name]
                })

            if valueType!="added" and len(diff)>0:
                self.currActivity.changes.append({
                    "field_name": field_name,
                    "new_value": None,
                    "old_value": ", ".join(map(unicode, Q.sort(diff))),
                    "attach_id": target.attach_id
                })

            return total | add




    def removeValues(self, total, remove, valueType, field_name, arrayDesc, target):
        if field_name == "flags":
            removeMe=[]
            for v in remove:
                flag = parse_bug_history_.makeFlag(v, 0, 0)

                found=self.findFlag(total, flag)
                if found is not None:
                    removeMe.append(CNV.object2JSON(found)) #FOR SOME REASON, REMOVAL BY OBJECT DOES NOT WORK
                else:
                    D.println("PROBLEM Unable to find {{type}} value: {{object}}.{{field_name}}: (All {{missing}}" + " not in : {{existing}})",{
                        "type":valueType,
                        "object":arrayDesc,
                        "field_name":field_name,
                        "missing":v,
                        "existing":target[field_name]
                    })

            total=[a for a in total if CNV.object2JSON(a) not in removeMe]
            if valueType=="added" and len(removeMe)>0:
                self.currActivity.changes.append({
                    "field_name": field_name,
                    "new_value": ", ".join(Q.sort([CNV.JSON2object(r).value for r in removeMe])),
                    "old_value": None,
                    "attach_id": target.attach_id
                })
            return total
        else:
            removed = total & remove
            diff = remove - total
            output = total - remove

            if valueType=="added":
                self.currActivity.changes.append({
                    "field_name": field_name,
                    "new_value": ", ".join(map(unicode, Q.sort(removed))),
                    "old_value": None,
                    "attach_id": target.attach_id
                })

            if len(diff)>0:
                D.println("PROBLEM Unable to find {{type}} value: {{object}}.{{field_name}}: (All {{missing}}" + " not in : {{existing}})",{
                    "type":valueType,
                    "object":arrayDesc,
                    "field_name":field_name,
                    "missing":diff,
                    "existing":target[field_name]
                })
            if "None" in output:
                D.println("PROBLEM error")

            return output



    @staticmethod
    def getMultiFieldValue(name, value):
        if value is None:
            return set()
        if name in MULTI_FIELDS:
            if name in NUMERIC_FIELDS:
                return set([int(s.strip()) for s in value.split(",") if s.strip()!=""])
            else:
                return set([s.strip() for s in value.split(",") if s.strip()!=""])

        return {value}

    
    def initializeAliases(self):
        try:
            BZ_ALIASES = File(self.settings.alias_file).read().split("\n")
            self.bzAliases ={}
            D.println("Initializing aliases")
            for alias in [s.split(";")[0].strip() for s in BZ_ALIASES]:
                if self.settings.debug: D.println("Adding alias '" + alias + "'")
                self.bzAliases[alias] = True
        except Exception, e:
            D.error("Can not init aliases", e)





