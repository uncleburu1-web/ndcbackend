# INEC geography (spec 16 §8, spec 19 §4)

`python manage.py load_geography <file.csv> --source "<where it came from>" --published-on YYYY-MM-DD --notes "<who checked it>"`
previews the file; add `--apply` to apply it. The same can be done from Admin → Geography → Files.

Polling units: `state_code,state,lga_code,lga,ward_code,ward,pu_code,polling_unit` (INEC's codes; ward = registration area).

Constituencies (`--kind senatorial_districts`, `federal_constituencies` or `state_constituencies`):
`state_code,code,name,lga_codes,ward_codes`, with codes separated by `;` and wards written `LGA-ward` (`07-05`).

The authoritative file is INEC's own published polling-unit list, converted once to CSV and spot-checked against INEC's
polling-unit locator (spec 19 §4.1). It is committed here, with its source and date, only after that check.

Test files (real LGA names; invented codes, wards and polling units), used by the tests and live checks:
- `sample.csv`: 5 polling units in Lagos and the FCT;
- `sample_v2.csv`: adds a unit, renames a ward and retires a unit;
- `sample_broken.csv`: a bad LGA code on line 3;
- `sample_senatorial_districts.csv`, `sample_federal_constituencies.csv`, `sample_state_constituencies.csv`.

After an apply the worker updates communities (spec 17) and search, and tells people whose unit was retired or moved.
