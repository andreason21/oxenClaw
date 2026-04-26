---
name: summarize
description: "Summarise arbitrary text at a target length using the agent's own model. Useful for compressing long inputs before feeding to other tools."
homepage: https://github.com/oxenclaw
openclaw:
  emoji: "📝"
---

# summarize

Pure-LLM summarisation. Hands `input_text` to a sub-LLM call with a
length hint (`short`/`medium`/`long`) and returns the summary as a
string. No external deps.
