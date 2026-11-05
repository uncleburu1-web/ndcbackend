"""
Adding a rule to tables that already hold data (spec 16 §6.3, §10.4).

`AddConstraintChecked` counts the rows that would break a check constraint first and stops the migration with the
constraint's name and the count (never the rows). Then it adds the constraint NOT VALID and validates it, so the table
is not locked for a full rewrite. Unique and other constraints fall back to Django's AddConstraint after the check.
"""
from django.db import migrations, models


class ConstraintViolated(Exception):
    pass


class AddConstraintChecked(migrations.AddConstraint):
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(app_label, self.model_name)
        if not self.allow_migrate_model(schema_editor.connection.alias, model):
            return
        c = self.constraint
        if isinstance(c, models.CheckConstraint) and schema_editor.connection.vendor == "postgresql":
            check = c._get_check_sql(model, schema_editor)
            table = schema_editor.quote_name(model._meta.db_table)
            with schema_editor.connection.cursor() as cur:
                cur.execute(f"SELECT count(*) FROM {table} WHERE NOT ({check})")
                bad = cur.fetchone()[0]
            if bad:
                raise ConstraintViolated(
                    f"{c.name}: {bad} existing row(s) break this rule. List their IDs with "
                    f"`python manage.py check_integrity --constraint {c.name}`, fix them through the app, then run migrate again.")
            name = schema_editor.quote_name(c.name)
            schema_editor.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({check}) NOT VALID")
            schema_editor.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")
            return
        super().database_forwards(app_label, schema_editor, from_state, to_state)

    def describe(self):
        return f"Add checked constraint {self.constraint.name} on model {self.model_name}"
