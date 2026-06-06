from collections import namedtuple
import csv
import io
import re
import sys
import urllib.parse
from pathlib import Path

import sqlglot
from sqlglot import exp


BASE_URL = (
    "https://app.powerbi.com/groups/3ab141f7-8611-40a1-93c1-574e6c9c778a/"
    "reports/a2590b37-4f3a-42d2-a801-35bd0d153a59/da516d60701ac009138b"
    "?experience=power-bi"
)
MAPPING_FILE = Path("powerbi_field_mapping.csv")

# Paste your SQL query here when running this file directly from VS Code.
SQL_QUERY = """SELECT
    p.[Project ID],
    p.[Project Description],
    d.[Year],
    SUM(p.[Year1 Net Revenue USD])              AS SumYear1_Net_Revenue_USD,
    SUM(p.[Incrementality Percentage Value])     AS SumIncrementality_Percentage_Value
FROM project p
JOIN project_country pc ON <join_key>
JOIN project_type pt    ON <join_key>
JOIN date_dim d         ON <date_join_key>
JOIN top_projects tp    ON tp.[Project ID] = p.[Project ID]
WHERE pc.Sector = 'PBNA'
  AND d.[Year] IN (2025, 2026)
  AND pt.[Project Sub Type] IN ('Breakthrough (in)', 'Breakthrough (out)', 'Reframe', 'Refresh')
  AND p.[Incrementality Percentage Value] >= 0.01
  AND p.[Project Status] NOT IN ('Cancelled', 'On-Hold')
GROUP BY ROLLUP (p.[Project ID], p.[Project Description], d.[Year])
ORDER BY
    GROUPING(p.[Project ID]) DESC,             -- grand total first
    SumYear1_Net_Revenue_USD DESC,
    p.[Project ID],
    p.[Project Description],
    d.[Year];
"""

INNO_SUBTYPES = {
    "Breakthrough (in)",
    "Breakthrough (out)",
    "Reframe",
    "Refresh",
}
CC_SUBTYPES = {
    "Artwork",
    "Brand Activation with no NPD",
    "Other",
    "Pack Price",
    "PEP + Positive Choices",
    "PEP + Sustainability",
    "Value Engineering",
}


FilterPart = namedtuple("FilterPart", ["table", "field", "operator", "values"])


def load_mapping_rows(mapping_file=MAPPING_FILE):
    """Load Power BI table and field mappings from the local CSV file."""
    if not mapping_file.exists():
        raise FileNotFoundError(
            f"Mapping file not found: {mapping_file}. "
            "Keep the local CSV with columns Table and Field in this folder."
        )
    text = mapping_file.read_text(encoding="utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def normalize_name(value):
    """Normalize table and field names so SQL and CSV labels can be matched."""
    value = value or ""
    value = re.sub(r"[_\s]+", " ", value.strip().lower())
    value = re.sub(r"[^a-z0-9 ]+", "", value)
    return re.sub(r"\s+", " ", value).strip()


def build_mapping(rows):
    """Build a lookup from normalized SQL column references to Power BI fields."""
    mapping = {}
    for row in rows:
        table = row.get("Table", "").strip()
        field = row.get("Field", "").strip()
        if not table or not field:
            continue
        mapping[(normalize_name(table), normalize_name(field))] = (table, field)

    # SQL/report aliases that are not one-to-one in the sheet.
    add_alias(mapping, "project", "Project_Product_Category", "project", "Project Product Category")
    add_alias(mapping, "project", "Project Launch Date", "date_dim", "Year")
    add_alias(mapping, "project", "Project Launch Date", "d", "Year")
    add_alias(mapping, "project_type", "Project Sub Type (Groups)", "project_type", "Project Sub Type")
    add_alias(mapping, "project_type", "Project Sub Type (Groups)", "pt", "Project Sub Type")
    return mapping


def add_alias(mapping, target_table, target_field, alias_table, alias_field):
    """Register an alternate SQL table or field name for an existing report field."""
    target = mapping.get((normalize_name(target_table), normalize_name(target_field)))
    if target:
        mapping[(normalize_name(alias_table), normalize_name(alias_field))] = target


def table_aliases(statement):
    """Return SQL aliases such as p -> project for one parsed SQL statement."""
    aliases = {}
    for table in statement.find_all(exp.Table):
        name = table.name
        alias = table.alias_or_name
        if name:
            aliases[normalize_name(name)] = name
        if alias and name:
            aliases[normalize_name(alias)] = name
    return aliases


def literal_value(node):
    """Convert a sqlglot literal expression into a Python value."""
    if isinstance(node, exp.Literal):
        if node.is_string:
            return node.this
        text = str(node.this)
        try:
            return int(text)
        except ValueError:
            try:
                return float(text)
            except ValueError:
                return text
    if isinstance(node, exp.Null):
        return None
    return node.sql(dialect="tsql")


def column_ref(node, aliases):
    """Return the resolved table and column name for a sqlglot column node."""
    if not isinstance(node, exp.Column):
        return None
    table = node.table or ""
    table = aliases.get(normalize_name(table), table)
    return table, node.name


def sql_operator(node, negated=False):
    """Map supported SQL comparison expressions to Power BI filter operators."""
    if isinstance(node, exp.EQ):
        return "ne" if negated else "eq"
    if isinstance(node, exp.NEQ):
        return "eq" if negated else "ne"
    if isinstance(node, exp.GTE):
        return "lt" if negated else "ge"
    if isinstance(node, exp.GT):
        return "le" if negated else "gt"
    if isinstance(node, exp.LTE):
        return "gt" if negated else "le"
    if isinstance(node, exp.LT):
        return "ge" if negated else "lt"
    if isinstance(node, exp.In):
        return "not in" if negated else "in"
    return None


def extract_conditions(node, aliases, negated=False):
    """Yield simple filter conditions from a parsed WHERE expression tree."""
    if node is None:
        return
    if isinstance(node, exp.Where):
        yield from extract_conditions(node.this, aliases, negated)
        return
    if isinstance(node, exp.Paren):
        yield from extract_conditions(node.this, aliases, negated)
        return
    if isinstance(node, exp.Not):
        yield from extract_conditions(node.this, aliases, not negated)
        return
    if isinstance(node, exp.And):
        yield from extract_conditions(node.left, aliases, negated)
        yield from extract_conditions(node.right, aliases, negated)
        return

    operator = sql_operator(node, negated)
    if not operator:
        return

    if isinstance(node, exp.In) and isinstance(node.this, exp.Case):
        values = tuple(literal_value(item) for item in node.expressions)
        yield ("project_type", "Project Sub Type (Groups)"), operator, values
        return

    ref = column_ref(node.this, aliases)
    if not ref:
        return

    if isinstance(node, exp.In):
        values = tuple(literal_value(item) for item in node.expressions)
    else:
        values = (literal_value(node.expression),)
    yield ref, operator, values


def mapped_filter(ref, operator, values, mapping):
    """Convert one SQL filter condition into a report-aware FilterPart."""
    table, field = ref
    target = mapping.get((normalize_name(table), normalize_name(field)))
    if not target and not table:
        matches = [
            target
            for (mapped_table, mapped_field), target in mapping.items()
            if mapped_field == normalize_name(field)
        ]
        if len(matches) == 1:
            target = matches[0]
    if not target:
        return None

    target_table, target_field = target
    values = convert_group_values(target_field, values)
    if not values:
        return None
    if len(values) == 1 and operator == "in":
        operator = "eq"
    return FilterPart(target_table, target_field, operator, tuple(values))


def convert_group_values(field, values):
    """Collapse project subtype values into the report's CC/INNO grouping values."""
    if normalize_name(field) != normalize_name("Project Sub Type (Groups)"):
        return values
    text_values = {str(value) for value in values if value is not None}
    groups = []
    if text_values & INNO_SUBTYPES or "INNO" in text_values:
        groups.append("INNO")
    if text_values & CC_SUBTYPES or "CC" in text_values:
        groups.append("CC")
    return tuple(groups or values)


def collect_filters(sql_query):
    """Parse SQL and collect the mapped Power BI filters found in WHERE clauses."""
    rows = load_mapping_rows()
    mapping = build_mapping(rows)
    parsed = sqlglot.parse(preprocess_sql(sql_query), read="tsql")

    filters = []
    seen = set()
    for statement in parsed:
        aliases = table_aliases(statement)
        for where in statement.find_all(exp.Where):
            for ref, operator, values in extract_conditions(where, aliases):
                part = mapped_filter(ref, operator, values, mapping)
                if part and part not in seen:
                    seen.add(part)
                    filters.append(part)
    return filters


def preprocess_sql(sql_query):
    """Make draft SQL placeholders parseable before handing the query to sqlglot."""
    sql_query = re.sub(r"<[^>]+>", "1 = 1", sql_query)
    sql_query = re.sub(
        r"CASE\s*/\*\s*same\s+CASE\s+expression\s+as\s+above\s*\*/\s*END",
        "NULL",
        sql_query,
        flags=re.IGNORECASE,
    )
    return sql_query


def sharepoint_encode_identifier(identifier):
    """Encode a Power BI field identifier using SharePoint-style _xNNNN_ escapes."""
    def replace_char(match):
        """Return the _xNNNN_ escape for one unsupported identifier character."""
        return f"_x{ord(match.group(0)):04x}_"

    return re.sub(r"[^A-Za-z0-9_]", replace_char, identifier.strip())


def format_value(value):
    """Format a Python value as a Power BI filter literal."""
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def format_filter(part):
    """Render one mapped filter as a Power BI filter expression fragment."""
    field_ref = f"{part.table}/{sharepoint_encode_identifier(part.field)}"
    if part.operator in {"in", "not in"}:
        values = ", ".join(format_value(value) for value in part.values)
        return f"{field_ref} {part.operator} ({values})"
    return f"{field_ref} {part.operator} {format_value(part.values[0])}"


def powerbi_filter_expression(filters):
    """Combine mapped filters into one Power BI filter expression."""
    return " and ".join(format_filter(part) for part in filters)


def powerbi_link_from_sql(sql_query, base_url=BASE_URL):
    """Build a Power BI dashboard URL with filters extracted from a SQL query."""
    filters = collect_filters(sql_query)
    if not filters:
        return base_url

    separator = "&" if "?" in base_url else "?"
    encoded = urllib.parse.quote(powerbi_filter_expression(filters), safe="")
    return f"{base_url}{separator}filter={encoded}"


def main():
    """Run the script from VS Code, a SQL file, a command-line query, or stdin."""
    if len(sys.argv) < 2:
        sql_query = SQL_QUERY
    elif sys.argv[1] == "-":
        sql_query = sys.stdin.read()
    else:
        input_text = " ".join(sys.argv[1:])
        input_path = Path(input_text)
        if input_path.exists() and input_path.is_file():
            sql_query = input_path.read_text(encoding="utf-8-sig")
        else:
            sql_query = input_text

    if not sql_query.strip():
        print("SQL query is empty.")
        return 1
    print(powerbi_link_from_sql(sql_query))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
