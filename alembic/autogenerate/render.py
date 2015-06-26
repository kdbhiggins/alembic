from sqlalchemy import schema as sa_schema, types as sqltypes, sql
from ..operations import ops
from ..util import compat
import re
from ..util.compat import string_types
from .. import util
from mako.pygen import PythonPrinter
from ..util.compat import StringIO


MAX_PYTHON_ARGS = 255

try:
    from sqlalchemy.sql.naming import conv

    def _render_gen_name(autogen_context, name):
        if isinstance(name, conv):
            return _f_name(_alembic_autogenerate_prefix(autogen_context), name)
        else:
            return name
except ImportError:
    def _render_gen_name(autogen_context, name):
        return name


def _indent(text):
    text = re.compile(r'^', re.M).sub("    ", text).strip()
    text = re.compile(r' +$', re.M).sub("", text)
    return text


def _render_migration_script(autogen_context, migration_script, template_args):
    opts = autogen_context['opts']
    imports = autogen_context['imports']
    template_args[opts['upgrade_token']] = _indent(_render_cmd_body(
        migration_script.upgrade_ops, autogen_context))
    template_args[opts['downgrade_token']] = _indent(_render_cmd_body(
        migration_script.downgrade_ops, autogen_context))
    template_args['imports'] = "\n".join(sorted(imports))


default_renderers = renderers = util.Dispatcher()


def _render_cmd_body(op_container, autogen_context):

    buf = StringIO()
    printer = PythonPrinter(buf)

    printer.writeline(
        "### commands auto generated by Alembic - "
        "please adjust! ###"
    )

    if not op_container.ops:
        printer.writeline("pass")
    else:
        for op in op_container.ops:
            lines = render_op(autogen_context, op)

            for line in lines:
                printer.writeline(line)

    printer.writeline("### end Alembic commands ###")

    return buf.getvalue()


def render_op(autogen_context, op):
    renderer = renderers.dispatch(op)
    lines = renderer(autogen_context, op)
    return lines


def render_op_text(autogen_context, op):
    return "\n".join(render_op(autogen_context, op))


@renderers.dispatch_for(ops.ModifyTableOps)
def _render_modify_table(autogen_context, op):
    opts = autogen_context['opts']
    render_as_batch = opts.get('render_as_batch', False)

    if op.ops:
        lines = []
        if render_as_batch:
            lines.append(
                "with op.batch_alter_table(%r, schema=%r) as batch_op:"
                % (op.table_name, op.schema)
            )
            autogen_context['batch_prefix'] = 'batch_op.'
        for t_op in op.ops:
            t_lines = render_op(autogen_context, t_op)
            lines.extend(t_lines)
        if render_as_batch:
            del autogen_context['batch_prefix']
            lines.append("")
        return lines
    else:
        return [
            "pass"
        ]


@renderers.dispatch_for(ops.CreateTableOp)
def _add_table(autogen_context, op):
    table = op.to_table()

    args = [col for col in
            [_render_column(col, autogen_context) for col in table.columns]
            if col] + \
        sorted([rcons for rcons in
                [_render_constraint(cons, autogen_context) for cons in
                 table.constraints]
                if rcons is not None
                ])

    if len(args) > MAX_PYTHON_ARGS:
        args = '*[' + ',\n'.join(args) + ']'
    else:
        args = ',\n'.join(args)

    text = "%(prefix)screate_table(%(tablename)r,\n%(args)s" % {
        'tablename': _ident(op.table_name),
        'prefix': _alembic_autogenerate_prefix(autogen_context),
        'args': args,
    }
    if op.schema:
        text += ",\nschema=%r" % _ident(op.schema)
    for k in sorted(op.kw):
        text += ",\n%s=%r" % (k.replace(" ", "_"), op.kw[k])
    text += "\n)"
    return [text]


@renderers.dispatch_for(ops.DropTableOp)
def _drop_table(autogen_context, op):
    text = "%(prefix)sdrop_table(%(tname)r" % {
        "prefix": _alembic_autogenerate_prefix(autogen_context),
        "tname": _ident(op.table_name)
    }
    if op.schema:
        text += ", schema=%r" % _ident(op.schema)
    text += ")"
    return [text]


@renderers.dispatch_for(ops.CreateIndexOp)
def _add_index(autogen_context, op):
    index = op.to_index()

    has_batch = 'batch_prefix' in autogen_context

    if has_batch:
        tmpl = "%(prefix)screate_index(%(name)r, [%(columns)s], "\
            "unique=%(unique)r%(kwargs)s)"
    else:
        tmpl = "%(prefix)screate_index(%(name)r, %(table)r, [%(columns)s], "\
            "unique=%(unique)r%(schema)s%(kwargs)s)"

    text = tmpl % {
        'prefix': _alembic_autogenerate_prefix(autogen_context),
        'name': _render_gen_name(autogen_context, index.name),
        'table': _ident(index.table.name),
        'columns': ", ".join(
            _get_index_rendered_expressions(index, autogen_context)),
        'unique': index.unique or False,
        'schema': (", schema=%r" % _ident(index.table.schema))
        if index.table.schema else '',
        'kwargs': (
            ', ' +
            ', '.join(
                ["%s=%s" %
                 (key, _render_potential_expr(val, autogen_context))
                 for key, val in index.kwargs.items()]))
        if len(index.kwargs) else ''
    }
    return [text]


@renderers.dispatch_for(ops.DropIndexOp)
def _drop_index(autogen_context, op):
    has_batch = 'batch_prefix' in autogen_context

    if has_batch:
        tmpl = "%(prefix)sdrop_index(%(name)r)"
    else:
        tmpl = "%(prefix)sdrop_index(%(name)r, "\
            "table_name=%(table_name)r%(schema)s)"

    text = tmpl % {
        'prefix': _alembic_autogenerate_prefix(autogen_context),
        'name': _render_gen_name(autogen_context, op.index_name),
        'table_name': _ident(op.table_name),
        'schema': ((", schema=%r" % _ident(op.schema))
                   if op.schema else '')
    }
    return [text]


@renderers.dispatch_for(ops.CreateUniqueConstraintOp)
def _add_unique_constraint(autogen_context, op):
    return [_uq_constraint(op.to_constraint(), autogen_context, True)]


@renderers.dispatch_for(ops.CreateForeignKeyOp)
def _add_fk_constraint(autogen_context, op):

    args = [
        repr(
            _render_gen_name(autogen_context, op.constraint_name)),
        repr(_ident(op.source_table)),
        repr(_ident(op.referent_table)),
        repr([_ident(col) for col in op.local_cols]),
        repr([_ident(col) for col in op.remote_cols])
    ]

    for k in (
        'source_schema', 'referent_schema',
        'onupdate', 'ondelete', 'initially', 'deferrable', 'use_alter'
    ):
        if k in op.kw:
            value = op.kw[k]
            if value is not None:
                args.append("%s=%r" % (k, value))

    return ["%(prefix)screate_foreign_key(%(args)s)" % {
        'prefix': _alembic_autogenerate_prefix(autogen_context),
        'args': ", ".join(args)
    }]


@renderers.dispatch_for(ops.CreatePrimaryKeyOp)
def _add_pk_constraint(constraint, autogen_context):
    raise NotImplementedError()


@renderers.dispatch_for(ops.CreateCheckConstraintOp)
def _add_check_constraint(constraint, autogen_context):
    raise NotImplementedError()


@renderers.dispatch_for(ops.DropConstraintOp)
def _drop_constraint(autogen_context, op):

    if 'batch_prefix' in autogen_context:
        template = "%(prefix)sdrop_constraint"\
            "(%(name)r, type_=%(type)r)"
    else:
        template = "%(prefix)sdrop_constraint"\
            "(%(name)r, '%(table_name)s'%(schema)s, type_=%(type)r)"

    text = template % {
        'prefix': _alembic_autogenerate_prefix(autogen_context),
        'name': _render_gen_name(
            autogen_context, op.constraint_name),
        'table_name': _ident(op.table_name),
        'type': op.constraint_type,
        'schema': (", schema='%s'" % _ident(op.schema))
        if op.schema else '',
    }
    return [text]


@renderers.dispatch_for(ops.AddColumnOp)
def _add_column(autogen_context, op):

    schema, tname, column = op.schema, op.table_name, op.column
    if 'batch_prefix' in autogen_context:
        template = "%(prefix)sadd_column(%(column)s)"
    else:
        template = "%(prefix)sadd_column(%(tname)r, %(column)s"
        if schema:
            template += ", schema=%(schema)r"
        template += ")"
    text = template % {
        "prefix": _alembic_autogenerate_prefix(autogen_context),
        "tname": tname,
        "column": _render_column(column, autogen_context),
        "schema": schema
    }
    return [text]


@renderers.dispatch_for(ops.DropColumnOp)
def _drop_column(autogen_context, op):

    schema, tname, column_name = op.schema, op.table_name, op.column_name

    if 'batch_prefix' in autogen_context:
        template = "%(prefix)sdrop_column(%(cname)r)"
    else:
        template = "%(prefix)sdrop_column(%(tname)r, %(cname)r"
        if schema:
            template += ", schema=%(schema)r"
        template += ")"

    text = template % {
        "prefix": _alembic_autogenerate_prefix(autogen_context),
        "tname": _ident(tname),
        "cname": _ident(column_name),
        "schema": _ident(schema)
    }
    return [text]


@renderers.dispatch_for(ops.AlterColumnOp)
def _alter_column(autogen_context, op):

    tname = op.table_name
    cname = op.column_name
    server_default = op.modify_server_default
    type_ = op.modify_type
    nullable = op.modify_nullable
    existing_type = op.existing_type
    existing_nullable = op.existing_nullable
    existing_server_default = op.existing_server_default
    schema = op.schema

    indent = " " * 11

    if 'batch_prefix' in autogen_context:
        template = "%(prefix)salter_column(%(cname)r"
    else:
        template = "%(prefix)salter_column(%(tname)r, %(cname)r"

    text = template % {
        'prefix': _alembic_autogenerate_prefix(
            autogen_context),
        'tname': tname,
        'cname': cname}
    text += ",\n%sexisting_type=%s" % (
        indent,
        _repr_type(existing_type, autogen_context))
    if server_default is not False:
        rendered = _render_server_default(
            server_default, autogen_context)
        text += ",\n%sserver_default=%s" % (indent, rendered)

    if type_ is not None:
        text += ",\n%stype_=%s" % (indent,
                                   _repr_type(type_, autogen_context))
    if nullable is not None:
        text += ",\n%snullable=%r" % (
            indent, nullable,)
    if existing_nullable is not None:
        text += ",\n%sexisting_nullable=%r" % (
            indent, existing_nullable)
    if existing_server_default:
        rendered = _render_server_default(
            existing_server_default,
            autogen_context)
        text += ",\n%sexisting_server_default=%s" % (
            indent, rendered)
    if schema and "batch_prefix" not in autogen_context:
        text += ",\n%sschema=%r" % (indent, schema)
    text += ")"
    return [text]


class _f_name(object):

    def __init__(self, prefix, name):
        self.prefix = prefix
        self.name = name

    def __repr__(self):
        return "%sf(%r)" % (self.prefix, _ident(self.name))


def _ident(name):
    """produce a __repr__() object for a string identifier that may
    use quoted_name() in SQLAlchemy 0.9 and greater.

    The issue worked around here is that quoted_name() doesn't have
    very good repr() behavior by itself when unicode is involved.

    """
    if name is None:
        return name
    elif compat.sqla_09 and isinstance(name, sql.elements.quoted_name):
        if compat.py2k:
            # the attempt to encode to ascii here isn't super ideal,
            # however we are trying to cut down on an explosion of
            # u'' literals only when py2k + SQLA 0.9, in particular
            # makes unit tests testing code generation very difficult
            try:
                return name.encode('ascii')
            except UnicodeError:
                return compat.text_type(name)
        else:
            return compat.text_type(name)
    elif isinstance(name, compat.string_types):
        return name


def _render_potential_expr(value, autogen_context, wrap_in_text=True):
    if isinstance(value, sql.ClauseElement):
        if compat.sqla_08:
            compile_kw = dict(compile_kwargs={'literal_binds': True})
        else:
            compile_kw = {}

        if wrap_in_text:
            template = "%(prefix)stext(%(sql)r)"
        else:
            template = "%(sql)r"

        return template % {
            "prefix": _sqlalchemy_autogenerate_prefix(autogen_context),
            "sql": compat.text_type(
                value.compile(dialect=autogen_context['dialect'],
                              **compile_kw)
            )
        }

    else:
        return repr(value)


def _get_index_rendered_expressions(idx, autogen_context):
    if compat.sqla_08:
        return [repr(_ident(getattr(exp, "name", None)))
                if isinstance(exp, sa_schema.Column)
                else _render_potential_expr(exp, autogen_context)
                for exp in idx.expressions]
    else:
        return [
            repr(_ident(getattr(col, "name", None))) for col in idx.columns]


def _uq_constraint(constraint, autogen_context, alter):
    opts = []

    has_batch = 'batch_prefix' in autogen_context

    if constraint.deferrable:
        opts.append(("deferrable", str(constraint.deferrable)))
    if constraint.initially:
        opts.append(("initially", str(constraint.initially)))
    if not has_batch and alter and constraint.table.schema:
        opts.append(("schema", _ident(constraint.table.schema)))
    if not alter and constraint.name:
        opts.append(
            ("name",
             _render_gen_name(autogen_context, constraint.name)))

    if alter:
        args = [
            repr(_render_gen_name(
                autogen_context, constraint.name))]
        if not has_batch:
            args += [repr(_ident(constraint.table.name))]
        args.append(repr([_ident(col.name) for col in constraint.columns]))
        args.extend(["%s=%r" % (k, v) for k, v in opts])
        return "%(prefix)screate_unique_constraint(%(args)s)" % {
            'prefix': _alembic_autogenerate_prefix(autogen_context),
            'args': ", ".join(args)
        }
    else:
        args = [repr(_ident(col.name)) for col in constraint.columns]
        args.extend(["%s=%r" % (k, v) for k, v in opts])
        return "%(prefix)sUniqueConstraint(%(args)s)" % {
            "prefix": _sqlalchemy_autogenerate_prefix(autogen_context),
            "args": ", ".join(args)
        }


def _user_autogenerate_prefix(autogen_context, target):
    prefix = autogen_context['opts']['user_module_prefix']
    if prefix is None:
        return "%s." % target.__module__
    else:
        return prefix


def _sqlalchemy_autogenerate_prefix(autogen_context):
    return autogen_context['opts']['sqlalchemy_module_prefix'] or ''


def _alembic_autogenerate_prefix(autogen_context):
    if 'batch_prefix' in autogen_context:
        return autogen_context['batch_prefix']
    else:
        return autogen_context['opts']['alembic_module_prefix'] or ''


def _user_defined_render(type_, object_, autogen_context):
    if 'opts' in autogen_context and \
            'render_item' in autogen_context['opts']:
        render = autogen_context['opts']['render_item']
        if render:
            rendered = render(type_, object_, autogen_context)
            if rendered is not False:
                return rendered
    return False


def _render_column(column, autogen_context):
    rendered = _user_defined_render("column", column, autogen_context)
    if rendered is not False:
        return rendered

    opts = []
    if column.server_default:
        rendered = _render_server_default(
            column.server_default, autogen_context
        )
        if rendered:
            opts.append(("server_default", rendered))

    if not column.autoincrement:
        opts.append(("autoincrement", column.autoincrement))

    if column.nullable is not None:
        opts.append(("nullable", column.nullable))

    # TODO: for non-ascii colname, assign a "key"
    return "%(prefix)sColumn(%(name)r, %(type)s, %(kw)s)" % {
        'prefix': _sqlalchemy_autogenerate_prefix(autogen_context),
        'name': _ident(column.name),
        'type': _repr_type(column.type, autogen_context),
        'kw': ", ".join(["%s=%s" % (kwname, val) for kwname, val in opts])
    }


def _render_server_default(default, autogen_context, repr_=True):
    rendered = _user_defined_render("server_default", default, autogen_context)
    if rendered is not False:
        return rendered

    if isinstance(default, sa_schema.DefaultClause):
        if isinstance(default.arg, compat.string_types):
            default = default.arg
        else:
            return _render_potential_expr(default.arg, autogen_context)

    if isinstance(default, string_types) and repr_:
        default = repr(re.sub(r"^'|'$", "", default))

    return default


def _repr_type(type_, autogen_context):
    rendered = _user_defined_render("type", type_, autogen_context)
    if rendered is not False:
        return rendered

    mod = type(type_).__module__
    imports = autogen_context.get('imports', None)
    if mod.startswith("sqlalchemy.dialects"):
        dname = re.match(r"sqlalchemy\.dialects\.(\w+)", mod).group(1)
        if imports is not None:
            imports.add("from sqlalchemy.dialects import %s" % dname)
        return "%s.%r" % (dname, type_)
    elif mod.startswith("sqlalchemy."):
        prefix = _sqlalchemy_autogenerate_prefix(autogen_context)
        return "%s%r" % (prefix, type_)
    else:
        prefix = _user_autogenerate_prefix(autogen_context, type_)
        return "%s%r" % (prefix, type_)


_constraint_renderers = util.Dispatcher()


def _render_constraint(constraint, autogen_context):
    renderer = _constraint_renderers.dispatch(constraint)
    return renderer(constraint, autogen_context)


@_constraint_renderers.dispatch_for(sa_schema.PrimaryKeyConstraint)
def _render_primary_key(constraint, autogen_context):
    rendered = _user_defined_render("primary_key", constraint, autogen_context)
    if rendered is not False:
        return rendered

    if not constraint.columns:
        return None

    opts = []
    if constraint.name:
        opts.append(("name", repr(
            _render_gen_name(autogen_context, constraint.name))))
    return "%(prefix)sPrimaryKeyConstraint(%(args)s)" % {
        "prefix": _sqlalchemy_autogenerate_prefix(autogen_context),
        "args": ", ".join(
            [repr(c.key) for c in constraint.columns] +
            ["%s=%s" % (kwname, val) for kwname, val in opts]
        ),
    }


def _fk_colspec(fk, metadata_schema):
    """Implement a 'safe' version of ForeignKey._get_colspec() that
    never tries to resolve the remote table.

    """
    colspec = fk._get_colspec()
    tokens = colspec.split(".")
    tname, colname = tokens[-2:]

    if metadata_schema is not None and len(tokens) == 2:
        table_fullname = "%s.%s" % (metadata_schema, tname)
    else:
        table_fullname = ".".join(tokens[0:-1])

    if fk.parent is not None and fk.parent.table is not None:
        # try to resolve the remote table and adjust for column.key
        parent_metadata = fk.parent.table.metadata
        if table_fullname in parent_metadata.tables:
            colname = _ident(
                parent_metadata.tables[table_fullname].c[colname].name)

    colspec = "%s.%s" % (table_fullname, colname)

    return colspec


def _populate_render_fk_opts(constraint, opts):

    if constraint.onupdate:
        opts.append(("onupdate", repr(constraint.onupdate)))
    if constraint.ondelete:
        opts.append(("ondelete", repr(constraint.ondelete)))
    if constraint.initially:
        opts.append(("initially", repr(constraint.initially)))
    if constraint.deferrable:
        opts.append(("deferrable", repr(constraint.deferrable)))
    if constraint.use_alter:
        opts.append(("use_alter", repr(constraint.use_alter)))


@_constraint_renderers.dispatch_for(sa_schema.ForeignKeyConstraint)
def _render_foreign_key(constraint, autogen_context):
    rendered = _user_defined_render("foreign_key", constraint, autogen_context)
    if rendered is not False:
        return rendered

    opts = []
    if constraint.name:
        opts.append(("name", repr(
            _render_gen_name(autogen_context, constraint.name))))

    _populate_render_fk_opts(constraint, opts)

    apply_metadata_schema = constraint.parent.metadata.schema
    return "%(prefix)sForeignKeyConstraint([%(cols)s], "\
        "[%(refcols)s], %(args)s)" % {
            "prefix": _sqlalchemy_autogenerate_prefix(autogen_context),
            "cols": ", ".join(
                "%r" % _ident(f.parent.name) for f in constraint.elements),
            "refcols": ", ".join(repr(_fk_colspec(f, apply_metadata_schema))
                                 for f in constraint.elements),
            "args": ", ".join(
                    ["%s=%s" % (kwname, val) for kwname, val in opts]
            ),
        }


@_constraint_renderers.dispatch_for(sa_schema.UniqueConstraint)
def _render_unique_constraint(constraint, autogen_context):
    rendered = _user_defined_render("unique", constraint, autogen_context)
    if rendered is not False:
        return rendered

    return _uq_constraint(constraint, autogen_context, False)


@_constraint_renderers.dispatch_for(sa_schema.CheckConstraint)
def _render_check_constraint(constraint, autogen_context):
    rendered = _user_defined_render("check", constraint, autogen_context)
    if rendered is not False:
        return rendered

    # detect the constraint being part of
    # a parent type which is probably in the Table already.
    # ideally SQLAlchemy would give us more of a first class
    # way to detect this.
    if constraint._create_rule and \
        hasattr(constraint._create_rule, 'target') and \
        isinstance(constraint._create_rule.target,
                   sqltypes.TypeEngine):
        return None
    opts = []
    if constraint.name:
        opts.append(
            (
                "name",
                repr(
                    _render_gen_name(
                        autogen_context, constraint.name))
            )
        )
    return "%(prefix)sCheckConstraint(%(sqltext)s%(opts)s)" % {
        "prefix": _sqlalchemy_autogenerate_prefix(autogen_context),
        "opts": ", " + (", ".join("%s=%s" % (k, v)
                                  for k, v in opts)) if opts else "",
        "sqltext": _render_potential_expr(
            constraint.sqltext, autogen_context, wrap_in_text=False)
    }


renderers = default_renderers.branch()
