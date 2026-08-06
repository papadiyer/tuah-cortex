Bro, aku nak kita bina Automated Memory Pipeline atau aku gelarkan "Jebat Cortex" untuk tingkatkan ingatan kau (Jebat/Hermes) berpandukan seni bina ini:

Architecture Flow:
Chat -> Conversation -> Memory Curator -> (Split to Experience & Knowledge)
- Experience -> Graphify (Relational/Code Graph)
- Knowledge -> Vector DB (Semantic Long-Term Memory)
Both merge at Context Builder -> Injected to Jebat Prompt.

Sila buka sub-task untuk Lekiu (Claude Code) supaya dia boleh mula scaffold codebase pipeline ni.

Syarat & Specification System:
1. Memory Curator Script:
   - Buat Python script untuk parse conversation log secara async.
   - Ekstrak fakta berciri "Knowledge" (fakta, config, preferences) dan simpan ke Vector DB / SQLite.
   - Ekstrak perkaitan berciri "Experience" (code architecture, dependencies, project structure) untuk di-index oleh Graphify/ripgrep fallback.

2. Context Builder Script:
   - Apabila prompt baharu diterima, query kedua-dua DB (Vector + Graph).
   - Filter & merge jawapan supaya tidak melebihi user_char_limit (1375) / memory_char_limit (2200).
   - Formatkan output dalam struktur JSON/Markdown untuk di-inject ke System Prompt.

3. Delegation Request to Lekiu:
   - Tulis spec file/task prompt yang jelas untuk Lekiu hasilkan folder structure.
### Cadangan Folder & Repo Structure (Bila Lekiu Build Nanti)

Lekiu boleh terus guna penamaan ni untuk susun folder projek:
    ~/.hermes/skills/jebat-cortex/
├── core/
│   ├── memory_curator.py    # Saring Experience vs Knowledge
│   ├── vector_store.py      # Vector DB (Semantic Knowledge)
│   ├── graph_store.py       # Graphify / Ripgrep Fallback (Experience)
│   └── context_builder.py   # Injector & Prompt Merger
├── config/
│   └── cortex_rules.json    # Had char_limit & threshold
├── tests/
└── run_cortex.sh            # Pipeline execution script

Sila draft task file untuk Lekiu dan tunjukkan alur binaan kod ini dulu, bro.