# os-perf – Last- und Performancetest für OpenSlides-Abstimmungen

Python-Tool, das den kritischsten Pfad von OpenSlides 4 testbar macht: **elektronische
Wahlen und Abstimmungen über Anträge**, auch mehrere gleichzeitig.

Das Tool basiert auf [OpenSlides/openslides-performance](https://github.com/OpenSlides/openslides-performance)
(Go) und dem Fork von [ostcar](https://github.com/ostcar/openslides-performance)
(Branch `feature/vote`, angepasst an den neuen Vote-Service). Die Verbindungsoptionen
sind kompatibel (`-d`, `-u`, `-p`, `--http`, `-4`). Anders als dort musst du hier nichts
vorbereiten: Das Tool legt alles selbst an und entfernt es am Ende wieder.
