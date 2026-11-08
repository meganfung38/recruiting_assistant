> Raw notes from the author's manual testing of an earlier version of the assistant, kept as a record of how it was tested.
> Each observation was traced in the turn log before code changed; the resulting fixes are listed in the failures table in
> `docs/design_notes.md`. The answers quoted here are not the current behaviour.

## 1
Where are we losing candidates for the Backend Engineer role?

Backend Engineer has been open 123 days, and the main candidate losses are in Technical Interview, with a smaller loss at Final Interview.

Stage	Reached	Moved on	Rejected	Withdrew	There now	Median days in stage: people who left / people there now (target)	Why people left
Resume Review	20	18	1	0	1	3 / 4 (5)	1 rejected: missing a required skill
Recruiter Screen	18	17	1	0	0	5 / — (7)	1 rejected: interview feedback below the bar
Technical Interview	17	5	2	5	5	13.5 / 16 (14)	2 rejected: interview feedback below the bar; 3 withdrew: waited too long to be scheduled; 1 withdrew: personal reasons; 1 withdrew: accepted another offer
Final Interview	5	0	2	1	2	6 / 8.5 (7)	2 rejected: interview feedback below the bar; 1 withdrew: accepted another offer
Technical Interview is the biggest bottleneck: it has the most losses, and 3 people there are already over the scheduling target now.

**NOTES**
- is the target hardcoded? where is 5, 7, 14 coming from - I thought scheduling target was 7? I feel like the median days in stage column is extremely confusing to interpret. Decide whether it is relevant enough to include or how to simplify presentation. 