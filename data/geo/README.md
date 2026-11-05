# INEC geography (spec 16 §8)

`python manage.py load_geography <file.csv> --source "<where it came from>" --published-on YYYY-MM-DD`

Columns: `state_code,state,lga_code,lga,ward_code,ward,pu_code,polling_unit` (INEC's codes; ward = registration area).

The authoritative file is INEC's own published polling-unit list, converted once to CSV and spot-checked against INEC's
polling-unit locator (spec 16 §19.1). It is committed here, with its source and date, only after that check.
`sample.csv` is a small test file (real LGA names; invented codes, wards and polling units) used by the tests and the live check.
