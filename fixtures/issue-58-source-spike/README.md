# Issue #58 source-integration spike fixtures

These are synthetic parser/mapping specifications, not production catalog data,
not business implementation, and not evidence that a source is licensed for
production. No downloaded security directory is copied into this folder.

- `nasdaq.synthetic.json`: pipe-delimited field order, a share-class symbol,
  an ETF, a test issue, duplicate keys, unknown exchange, and preferred-symbol
  punctuation. Build a header, rows, and a File Creation Time trailer. Remove
  test issues before catalog mapping. Prefixes in expected_keys are notation
  for `(kind, code)`, never price-storage strings.
- `hkex.synthetic.json`: workbook construction specification and field dictionary.
  Pad each synthetic row to 18 columns; write title/date/header followed by rows
  on ListOfSecurities. To reproduce the observed dimension trap, place rows
  beyond row 8, then set worksheet dimension metadata to A1:R8. A streaming
  reader must reset/untrust dimensions and read the XML to EOF. The workbook
  generator itself is not shipped as application code.
- `fund.synthetic.json`: five-string array rows wrapped in `var r = ...;`.
  Parse the array as data, never execute JavaScript. Unicode-escaped category
  labels exercise decoding without introducing non-English prose. Names are
  invented; sample codes are not assertions about real securities.
- `download-evidence.json`: response metadata, hashes, and observed counts from
  the one-time 2026-09-05 probe. Hashes identify the inspected payload only;
  they are not expected hashes for a changing daily feed.

All fixtures are deliberately synthetic because HKEX restricts automated
access/directory compilation and AKShare/Eastmoney do not provide established
commercial redistribution permission for this use. Nasdaq's page distinguishes
different licensing scopes; do not infer a blanket license for every file.
See the source links and licensing findings in the issue comment.

Proposed offline mutation checks: empty/truncated or HTML payload, wrong field
count, malformed timestamp, invalid UTF-8, unexpected JS after the array,
conflicting canonical key, unknown category/exchange, and a per-scope row-count
drop below 80% of the last accepted baseline. These are future adapter tests;
this spike only validated the JSON fixture structures and the downloaded data
with scratch parsers, not a production activation implementation.

Refresh and validation are separate. User requests only query local snapshots.
A miss never changes a security, a user name, pricing mode, or capture support.
Missing coverage is unavailable, not not_found. Keep old snapshots on rejected
refreshes and apply the PO-approved per-source operational alert deduplication.

These files are spike deliverables submitted for review on a dedicated branch
(Refs #58). They are not merged, not activated in any environment, and not
installed into the runtime; they establish no production source, coverage, or
identity clearance for #58.
