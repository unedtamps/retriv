"""Describing the pictures inside a document.

A PDF can carry information that no text layer holds: a chart's values, a
diagram's arrows, a photograph's subject. The parser sees those regions as
pixels and drops them, so a question about the chart cannot be answered from
the document at all.

This package is the stage that closes that gap. It sends a picture to a
multimodal model behind an OpenAI-compatible API and gets back a description
in words, which the pipeline folds into the chunk that holds the picture and
embeds with the same text embedding model as everything else.

Two rules shape the code here. The model is a remote service, so a call can
fail for reasons that have nothing to do with the document: it is told apart
from a failure caused by the document, and only the first kind is retried.
And the answer is model output, not source text, so it is labelled as such
wherever it lands.
"""
