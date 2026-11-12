from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0007_backfill_deactivation_kind")]
    operations = [
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("is_active", True), ("deactivation_kind", "")),
                    models.Q(
                        ("is_active", False),
                        models.Q(("deactivation_kind", ""), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="accounts_deactivation_kind_matches",
            ),
        ),
    ]
