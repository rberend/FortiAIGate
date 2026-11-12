# FortiAnalyzer log parser for FortiAIGate 8.0.2

`FortiAIGate-8.0.2-Log-Parser.json` is a FortiAnalyzer custom log parser for the request logs FortiAIGate 8.0.2 sends over syslog.

## The log format

FortiAIGate 8.0.2 sends one RFC 5424 syslog message per gateway request, with every field in a structured-data element:

```
<pri>1 2026-11-12T18:35:47.260424Z 3ddb76502152 fortiaigate - 000000000030 [fortiaigate@8888 log_version="1" request_id="…" call_type="openai.chat_completions" … action="DENY" verdict="True" violation_detail="[{'action': 'DENY', 'details': 'Matched rules: …', 'detector_name': 'lab-secrets-regex', 'detector_type': 'REGEX', 'direction': 'input', …}\]" … requester_ip_address="192.168.1.135" …]
```

Parsers written for earlier FortiAIGate builds, which matched `log_id=20100001` and `ai_guard_uuid`, do not match this format.

## What the parser does

1. **Matches** syslog lines that contain the `fortiaigate@8888` structured-data element and a `request_id`.
2. **Cleans escapes.** RFC 5424 escapes `"` and `]` inside values as `\"` and `\]`. Left alone, `match_logfmt` ends a value at the first `\"`, so a prompt or system prompt that contains a double quote cuts that field short and shifts every field after it. The parser turns `\"` into `'` and `\]` into `]` first.
3. **Parses** the `key="value"` pairs with `match_logfmt` and normalizes them (table below).
4. **Sets `event_action` to ALLOW** for allowed requests, which carry no `action` field.
5. **On a violation** (`verdict` true), extracts the first detector's name, type, direction, details and ID from `violation_detail` into the threat fields.

| Normalized field | FortiAIGate field |
| --- | --- |
| `event_uuid` | `request_id` |
| `event_id` | syslog MSGID (sequence number) |
| `event_action` | `action` (DENY), or ALLOW when allowed |
| `event_outcome` | `outcome` (`success`, `request_fatal`) |
| `event_severity` | syslog level |
| `event_policy` | `policy_ids` |
| `threat_name` / `threat_type` / `threat_direction` / `threat_message` | `detector_name` / `detector_type` / `direction` / `details` from `violation_detail` |
| `src_ip`, `host_ip` | `requester_ip_address` |
| `user_id` / `event_ref` | `virtual_key_id` |
| `app_name` / `app_ver` / `app_ref` | `model` / `model_id` / `custom_llm_provider` |
| `event_message` / `http_response_body` | `prompt_original` / `response_original` |
| `dns_query` | `system_prompt` (kept from the earlier parser's mapping) |
| `net_sentbytes` / `net_recvbytes` / `event_count` | prompt / completion / total tokens |
| `threat_score` | `spend` |
| `event_duration` / `http_response_time` | total / LLM request duration (ms) |
| `app_data` | organization, key source, credential, verdict, detector, guard timings, cached tokens, metadata and more |

## Import and check

1. In FortiAnalyzer 8.0.2, open the log parsers page and import `FortiAIGate-8.0.2-Log-Parser.json` as a custom log parser.
2. Make sure the FortiAIGate syslog device (shown as `SYSLOG-C0A8018B` for 192.168.1.139) is assigned to it. FortiAnalyzer assigns matching devices automatically once the pattern matches.
3. Send a few requests, including the console's canary test, then in **Log View** filter on `Data Parser Name = FortiAIGate 8.0.2 Log Parser` and check that `event_action`, `threat_name` and `event_message` are filled.

## Things to verify on first import

- **The escape cleanup.** It relies on the parser's string literals treating `\\` and `\"` as escapes (`replace(msg_body, "\\\"", "'")`). If prompts containing double quotes still come out cut short, that is the line to adjust.
- **Only the first violation** is extracted into the threat fields; the full list stays in `app_data.violation_detail`.
- **Very long prompts or replies** may exceed the syslog message size limit and arrive truncated. The match pattern does not require the closing `]`, so truncated records still parse up to the cut.

The logic was checked offline against rebuilt raw messages (the matcher, the escape cleanup, a logfmt split, and the violation extraction), not on a FortiAnalyzer.
