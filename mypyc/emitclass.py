"""Code generation for native classes and related wrappers."""


from typing import Optional, List, Tuple, Dict, Callable, Mapping, Set
from collections import OrderedDict

from mypyc.common import NATIVE_PREFIX, PREFIX, REG_PREFIX
from mypyc.emit import Emitter
from mypyc.emitfunc import native_function_header, native_getter_name, native_setter_name
from mypyc.emitwrapper import (
    generate_dunder_wrapper, generate_hash_wrapper, generate_richcompare_wrapper,
    generate_bool_wrapper, generate_get_wrapper,
)
from mypyc.ops import (
    ClassIR, FuncIR, FuncDecl, RType, RTuple, object_rprimitive,
    VTableMethod, VTableEntries,
    FUNC_STATICMETHOD, FUNC_CLASSMETHOD,
)
from mypyc.sametype import is_same_type
from mypyc.namegen import NameGenerator


def native_slot(cl: ClassIR, fn: FuncIR, emitter: Emitter) -> str:
    return '{}{}'.format(NATIVE_PREFIX, fn.cname(emitter.names))


def wrapper_slot(cl: ClassIR, fn: FuncIR, emitter: Emitter) -> str:
    return '{}{}'.format(PREFIX, fn.cname(emitter.names))


# We maintain a table from dunder function names to struct slots they
# correspond to and functions that generate a wrapper (if necessary)
# and return the function name to stick in the slot.
# TODO: Add remaining dunder methods
SlotGenerator = Callable[[ClassIR, FuncIR, Emitter], str]
SlotTable = Mapping[str, Tuple[str, SlotGenerator]]

SLOT_DEFS = {
    '__init__': ('tp_init', lambda c, t, e: generate_init_for_class(c, t, e)),
    '__call__': ('tp_call', wrapper_slot),
    '__str__': ('tp_str', native_slot),
    '__repr__': ('tp_repr', native_slot),
    '__next__': ('tp_iternext', native_slot),
    '__iter__': ('tp_iter', native_slot),
    '__hash__': ('tp_hash', generate_hash_wrapper),
    '__get__': ('tp_descr_get', generate_get_wrapper),
}  # type: SlotTable

AS_MAPPING_SLOT_DEFS = {
    '__getitem__': ('mp_subscript', generate_dunder_wrapper),
}  # type: SlotTable

AS_NUMBER_SLOT_DEFS = {
    '__bool__': ('nb_bool', generate_bool_wrapper),
}  # type: SlotTable

AS_ASYNC_SLOT_DEFS = {
    '__await__': ('am_await', native_slot),
    '__aiter__': ('am_aiter', native_slot),
    '__anext__': ('am_anext', native_slot),
}  # type: SlotTable

SIDE_TABLES = [
    ('as_mapping', 'PyMappingMethods', AS_MAPPING_SLOT_DEFS),
    ('as_number', 'PyNumberMethods', AS_NUMBER_SLOT_DEFS),
    ('as_async', 'PyAsyncMethods', AS_ASYNC_SLOT_DEFS),
]


def generate_slots(cl: ClassIR, table: SlotTable, emitter: Emitter) -> Dict[str, str]:
    fields = OrderedDict()  # type: Dict[str, str]
    for name, (slot, generator) in table.items():
        method = cl.get_method(name)
        if method:
            fields[slot] = generator(cl, method, emitter)

    return fields


def generate_class_type_decl(cl: ClassIR, c_emitter: Emitter, emitter: Emitter) -> None:
    c_emitter.emit_line('PyTypeObject *{};'.format(emitter.type_struct_name(cl)))
    emitter.emit_line('extern PyTypeObject *{};'.format(emitter.type_struct_name(cl)))
    emitter.emit_line()
    generate_object_struct(cl, emitter)
    emitter.emit_line()
    declare_native_getters_and_setters(cl, emitter)
    generate_full = not cl.is_trait and not cl.builtin_base
    if generate_full:
        emitter.emit_line('{};'.format(native_function_header(cl.ctor, emitter)))


def generate_class(cl: ClassIR, module: str, emitter: Emitter) -> None:
    """Generate C code for a class.

    This is the main entry point to the module.
    """
    name = cl.name
    name_prefix = cl.name_prefix(emitter.names)

    setup_name = '{}_setup'.format(name_prefix)
    new_name = '{}_new'.format(name_prefix)
    members_name = '{}_members'.format(name_prefix)
    getseters_name = '{}_getseters'.format(name_prefix)
    vtable_name = '{}_vtable'.format(name_prefix)
    traverse_name = '{}_traverse'.format(name_prefix)
    clear_name = '{}_clear'.format(name_prefix)
    dealloc_name = '{}_dealloc'.format(name_prefix)
    methods_name = '{}_methods'.format(name_prefix)
    vtable_setup_name = '{}_trait_vtable_setup'.format(name_prefix)

    fields = OrderedDict()  # type: Dict[str, str]
    fields['tp_name'] = '"{}"'.format(name)

    generate_full = not cl.is_trait and not cl.builtin_base
    needs_getseters = not cl.is_generated

    if generate_full:
        fields['tp_new'] = new_name
        fields['tp_dealloc'] = '(destructor){}_dealloc'.format(name_prefix)
        fields['tp_traverse'] = '(traverseproc){}_traverse'.format(name_prefix)
        fields['tp_clear'] = '(inquiry){}_clear'.format(name_prefix)
    if needs_getseters:
        fields['tp_getset'] = getseters_name
    fields['tp_methods'] = methods_name

    def emit_line() -> None:
        emitter.emit_line()

    emit_line()

    # If the class has a method to initialize default attribute
    # values, we need to call it during initialization.
    defaults_fn = cl.get_method('__mypyc_defaults_setup')

    # If there is a __init__ method, we'll use it in the native constructor.
    init_fn = cl.get_method('__init__')

    # Fill out slots in the type object from dunder methods.
    fields.update(generate_slots(cl, SLOT_DEFS, emitter))

    # Fill out dunder methods that live in tables hanging off the side.
    for table_name, type, slot_defs in SIDE_TABLES:
        slots = generate_slots(cl, slot_defs, emitter)
        if slots:
            table_struct_name = generate_side_table_for_class(cl, table_name, type, slots, emitter)
            fields['tp_{}'.format(table_name)] = '&{}'.format(table_struct_name)

    richcompare_name = generate_richcompare_wrapper(cl, emitter)
    if richcompare_name:
        fields['tp_richcompare'] = richcompare_name

    # If the class inherits from python, make space for a __dict__
    struct_name = cl.struct_name(emitter.names)
    if cl.builtin_base:
        base_size = 'sizeof({})'.format(cl.builtin_base)
    elif cl.is_trait:
        base_size = 'sizeof(PyObject)'
    else:
        base_size = 'sizeof({})'.format(struct_name)
    # Since our types aren't allocated using type() we need to
    # populate these fields ourselves if we want them to have correct
    # values. PyType_Ready will inherit the offsets from tp_base but
    # that isn't what we want.

    # XXX: there is no reason for the __weakref__ stuff to be mixed up with __dict__
    if cl.has_dict:
        # __dict__ lives right after the struct and __weakref__ lives right after that
        # TODO: They should get members in the struct instead of doing this nonsense.
        weak_offset = '{} + sizeof(PyObject *)'.format(base_size)
        emitter.emit_lines(
            'PyMemberDef {}[] = {{'.format(members_name),
            '{{"__dict__", T_OBJECT_EX, {}, 0, NULL}},'.format(base_size),
            '{{"__weakref__", T_OBJECT_EX, {}, 0, NULL}},'.format(weak_offset),
            '{0}',
            '};',
        )

        fields['tp_members'] = members_name
        fields['tp_basicsize'] = '{} + 2*sizeof(PyObject *)'.format(base_size)
        fields['tp_dictoffset'] = base_size
        fields['tp_weaklistoffset'] = weak_offset
    else:
        fields['tp_basicsize'] = base_size

    if generate_full:
        emitter.emit_line('static PyObject *{}(void);'.format(setup_name))
        assert cl.ctor is not None
        emitter.emit_line(native_function_header(cl.ctor, emitter) + ';')

        emit_line()
        generate_new_for_class(cl, new_name, vtable_name, setup_name, emitter)
        emit_line()
        generate_traverse_for_class(cl, traverse_name, emitter)
        emit_line()
        generate_clear_for_class(cl, clear_name, emitter)
        emit_line()
        generate_dealloc_for_class(cl, dealloc_name, clear_name, emitter)
        emit_line()
        generate_native_getters_and_setters(cl, emitter)
        vtable_name = generate_vtables(cl, vtable_name, emitter)
        emit_line()
    if needs_getseters:
        generate_getseter_declarations(cl, emitter)
        emit_line()
        generate_getseters_table(cl, getseters_name, emitter)
        emit_line()
    generate_methods_table(cl, methods_name, emitter)
    emit_line()

    flags = ['Py_TPFLAGS_DEFAULT', 'Py_TPFLAGS_HEAPTYPE', 'Py_TPFLAGS_BASETYPE']
    if generate_full:
        flags.append('Py_TPFLAGS_HAVE_GC')
    fields['tp_flags'] = ' | '.join(flags)

    emitter.emit_line("static PyTypeObject {}_template_ = {{".format(emitter.type_struct_name(cl)))
    emitter.emit_line("PyVarObject_HEAD_INIT(NULL, 0)")
    for field, value in fields.items():
        emitter.emit_line(".{} = {},".format(field, value))
    emitter.emit_line("};")
    emitter.emit_line("static PyTypeObject *{t}_template = &{t}_template_;".format(
        t=emitter.type_struct_name(cl)))

    emitter.emit_line()
    generate_trait_vtable_setup(cl, vtable_setup_name, vtable_name, emitter)
    if generate_full:
        generate_setup_for_class(cl, setup_name, defaults_fn, vtable_name, emitter)
        emitter.emit_line()
        generate_constructor_for_class(
            cl, cl.ctor, init_fn, setup_name, vtable_name, emitter)
        emitter.emit_line()
    if needs_getseters:
        generate_getseters(cl, emitter)


def getter_name(cl: ClassIR, attribute: str, names: NameGenerator) -> str:
    return names.private_name(cl.module_name, '{}_get{}'.format(cl.name, attribute))


def setter_name(cl: ClassIR, attribute: str, names: NameGenerator) -> str:
    return names.private_name(cl.module_name, '{}_set{}'.format(cl.name, attribute))


def generate_object_struct(cl: ClassIR, emitter: Emitter) -> None:
    emitter.emit_lines('typedef struct {',
                       'PyObject_HEAD',
                       'CPyVTableItem *vtable;')
    seen_attrs = set()  # type: Set[Tuple[str, RType]]
    for base in reversed(cl.base_mro):
        if not base.is_trait:
            for attr, rtype in base.attributes.items():
                if (attr, rtype) not in seen_attrs:
                    emitter.emit_line('{}{};'.format(emitter.ctype_spaced(rtype),
                                                     emitter.attr(attr)))
                    seen_attrs.add((attr, rtype))
    emitter.emit_line('}} {};'.format(cl.struct_name(emitter.names)))


def declare_native_getters_and_setters(cl: ClassIR,
                                       emitter: Emitter) -> None:
    for attr, rtype in cl.attributes.items():
        emitter.emit_line('{}{}({} *self);'.format(emitter.ctype_spaced(rtype),
                                                   native_getter_name(cl, attr, emitter.names),
                                                   cl.struct_name(emitter.names)))
        emitter.emit_line(
            'bool {}({} *self, {}value);'.format(native_setter_name(cl, attr, emitter.names),
                                                 cl.struct_name(emitter.names),
                                                 emitter.ctype_spaced(rtype)))


def generate_native_getters_and_setters(cl: ClassIR,
                                        emitter: Emitter) -> None:
    for attr, rtype in cl.attributes.items():
        attr_field = emitter.attr(attr)

        # Native getter
        emitter.emit_line('{}{}({} *self)'.format(emitter.ctype_spaced(rtype),
                                                  native_getter_name(cl, attr, emitter.names),
                                                  cl.struct_name(emitter.names)))
        emitter.emit_line('{')
        if rtype.is_refcounted:
            emit_undefined_check(rtype, emitter, attr_field, '==')
            emitter.emit_lines(
                'PyErr_SetString(PyExc_AttributeError, "attribute {} of {} undefined");'.format(
                    repr(attr), repr(cl.name)),
                '} else {')
            emitter.emit_inc_ref('self->{}'.format(attr_field), rtype)
            emitter.emit_line('}')
        emitter.emit_line('return self->{};'.format(attr_field))
        emitter.emit_line('}')
        emitter.emit_line()
        # Native setter
        emitter.emit_line(
            'bool {}({} *self, {}value)'.format(native_setter_name(cl, attr, emitter.names),
                                                cl.struct_name(emitter.names),
                                                emitter.ctype_spaced(rtype)))
        emitter.emit_line('{')
        if rtype.is_refcounted:
            emit_undefined_check(rtype, emitter, attr_field, '!=')
            emitter.emit_dec_ref('self->{}'.format(attr_field), rtype)
            emitter.emit_line('}')
        # This steal the reference to src, so we don't need to increment the arg
        emitter.emit_lines('self->{} = value;'.format(attr_field),
                           'return 1;',
                           '}')
        emitter.emit_line()


def generate_vtables(base: ClassIR,
                     vtable_name: str,
                     emitter: Emitter) -> str:
    """Emit the vtables for a class.

    This includes both the primary vtable and any trait implementation vtables.

    Returns the expression to use to refer to the vtable, which might be
    different than the name, if there are trait vtables."""

    subtables = []
    for trait, vtable in base.trait_vtables.items():
        name = '{}_{}_trait_vtable'.format(
            base.name_prefix(emitter.names), trait.name_prefix(emitter.names))
        generate_vtable(vtable, name, emitter, [])
        subtables.append((trait, name))

    generate_vtable(base.vtable_entries, vtable_name, emitter, subtables)

    return vtable_name if not subtables else "{} + {}".format(vtable_name, len(subtables) * 2)


def generate_vtable(entries: VTableEntries,
                    vtable_name: str,
                    emitter: Emitter,
                    subtables: List[Tuple[ClassIR, str]]) -> None:
    emitter.emit_line('static CPyVTableItem {}[] = {{'.format(vtable_name))
    if subtables:
        emitter.emit_line('/* Array of trait vtables */')
        for trait, table in subtables:
            # N.B: C only lets us store constant values. We do a nasty hack of
            # storing a pointer to the location, which we will then dynamically
            # patch up on module load in CPy_FixupTraitVtable.
            emitter.emit_line('(CPyVTableItem)&{}, (CPyVTableItem){},'.format(
                emitter.type_struct_name(trait), table))
        emitter.emit_line('/* Start of real vtable */')

    for entry in entries:
        if isinstance(entry, VTableMethod):
            emitter.emit_line('(CPyVTableItem){}{},'.format(NATIVE_PREFIX,
                                                            entry.method.cname(emitter.names)))
        else:
            cl, attr, is_setter = entry
            namer = native_setter_name if is_setter else native_getter_name
            emitter.emit_line('(CPyVTableItem){},'.format(namer(cl, attr, emitter.names)))
    # msvc doesn't allow empty arrays; maybe allowing them at all is an extension?
    if not entries:
        emitter.emit_line('NULL')
    emitter.emit_line('};')


def generate_trait_vtable_setup(cl: ClassIR,
                                vtable_setup_name: str,
                                vtable_name: str,
                                emitter: Emitter) -> None:
    """Generate a native function that fixes up the trait vtables of a class.

    This needs to be called before a class is used.
    """
    emitter.emit_line('static bool')
    emitter.emit_line('{}{}(void)'.format(NATIVE_PREFIX, vtable_setup_name))
    emitter.emit_line('{')
    if cl.trait_vtables and not cl.is_trait:
        emitter.emit_lines('CPy_FixupTraitVtable({}_vtable, {});'.format(
            cl.name_prefix(emitter.names), len(cl.trait_vtables)))
    emitter.emit_line('return 1;')
    emitter.emit_line('}')


def generate_setup_for_class(cl: ClassIR,
                             func_name: str,
                             defaults_fn: Optional[FuncIR],
                             vtable_name: str,
                             emitter: Emitter) -> None:
    """Generate a native function that allocates an instance of a class."""
    emitter.emit_line('static PyObject *')
    emitter.emit_line('{}(void)'.format(func_name))
    emitter.emit_line('{')
    emitter.emit_line('{} *self;'.format(cl.struct_name(emitter.names)))
    emitter.emit_line('self = ({struct} *){type_struct}->tp_alloc({type_struct}, 0);'.format(
        struct=cl.struct_name(emitter.names),
        type_struct=emitter.type_struct_name(cl)))
    emitter.emit_line('if (self == NULL)')
    emitter.emit_line('    return NULL;')
    emitter.emit_line('self->vtable = {};'.format(vtable_name))
    for base in reversed(cl.base_mro):
        for attr, rtype in base.attributes.items():
            emitter.emit_line('self->{} = {};'.format(
                emitter.attr(attr), emitter.c_undefined_value(rtype)))

    # Initialize attributes to default values, if necessary
    if defaults_fn is not None:
        emitter.emit_lines(
            'if ({}{}((PyObject *)self) == 0) {{'.format(
                NATIVE_PREFIX, defaults_fn.cname(emitter.names)),
            'Py_DECREF(self);',
            'return NULL;',
            '}')

    emitter.emit_line('return (PyObject *)self;')
    emitter.emit_line('}')


def generate_constructor_for_class(cl: ClassIR,
                                   fn: FuncDecl,
                                   init_fn: Optional[FuncIR],
                                   setup_name: str,
                                   vtable_name: str,
                                   emitter: Emitter) -> None:
    """Generate a native function that allocates and initializes an instance of a class."""
    emitter.emit_line('{}'.format(native_function_header(fn, emitter)))
    emitter.emit_line('{')
    emitter.emit_line('PyObject *self = {}();'.format(setup_name))
    emitter.emit_line('if (self == NULL)')
    emitter.emit_line('    return NULL;')
    if init_fn is not None:
        args = ', '.join(['self'] + [REG_PREFIX + arg.name for arg in fn.sig.args])
        emitter.emit_line('char res = {}{}({});'.format(
            NATIVE_PREFIX, init_fn.cname(emitter.names), args))
        emitter.emit_line('if (res == 2) {')
        emitter.emit_line('Py_DECREF(self);')
        emitter.emit_line('return NULL;')
        emitter.emit_line('}')
    emitter.emit_line('return self;')
    emitter.emit_line('}')


def generate_init_for_class(cl: ClassIR,
                            init_fn: FuncIR,
                            emitter: Emitter) -> str:
    """Generate an init function suitable for use as tp_init.

    tp_init needs to be a function that returns an int, and our
    __init__ methods return a PyObject. Translate NULL to -1,
    everything else to 0.
    """
    func_name = '{}_init'.format(cl.name_prefix(emitter.names))

    emitter.emit_line('static int')
    emitter.emit_line(
        '{}(PyObject *self, PyObject *args, PyObject *kwds)'.format(func_name))
    emitter.emit_line('{')
    emitter.emit_line('return {}{}(self, args, kwds) != NULL ? 0 : -1;'.format(
        PREFIX, init_fn.cname(emitter.names)))
    emitter.emit_line('}')

    return func_name


def generate_new_for_class(cl: ClassIR,
                           func_name: str,
                           vtable_name: str,
                           setup_name: str,
                           emitter: Emitter) -> None:
    emitter.emit_line('static PyObject *')
    emitter.emit_line(
        '{}(PyTypeObject *type, PyObject *args, PyObject *kwds)'.format(func_name))
    emitter.emit_line('{')
    # TODO: Check and unbox arguments
    emitter.emit_line('if (type != {}) {{'.format(emitter.type_struct_name(cl)))
    emitter.emit_line(
        'PyErr_SetString(PyExc_TypeError, "interpreted classes cannot inherit from compiled");')
    emitter.emit_line('return NULL;')
    emitter.emit_line('}')

    emitter.emit_line('return {}();'.format(setup_name))
    emitter.emit_line('}')


def generate_traverse_for_class(cl: ClassIR,
                                func_name: str,
                                emitter: Emitter) -> None:
    """Emit function that performs cycle GC traversal of an instance."""
    emitter.emit_line('static int')
    emitter.emit_line('{}({} *self, visitproc visit, void *arg)'.format(
        func_name,
        cl.struct_name(emitter.names)))
    emitter.emit_line('{')
    for base in reversed(cl.base_mro):
        for attr, rtype in base.attributes.items():
            emitter.emit_gc_visit('self->{}'.format(emitter.attr(attr)), rtype)
    if cl.has_dict:
        struct_name = cl.struct_name(emitter.names)
        # __dict__ lives right after the struct and __weakref__ lives right after that
        emitter.emit_gc_visit('*((PyObject **)((char *)self + sizeof({})))'.format(
            struct_name), object_rprimitive)
        emitter.emit_gc_visit(
            '*((PyObject **)((char *)self + sizeof(PyObject *) + sizeof({})))'.format(
                struct_name),
            object_rprimitive)
    emitter.emit_line('return 0;')
    emitter.emit_line('}')


def generate_clear_for_class(cl: ClassIR,
                             func_name: str,
                             emitter: Emitter) -> None:
    emitter.emit_line('static int')
    emitter.emit_line('{}({} *self)'.format(func_name, cl.struct_name(emitter.names)))
    emitter.emit_line('{')
    for base in reversed(cl.base_mro):
        for attr, rtype in base.attributes.items():
            emitter.emit_gc_clear('self->{}'.format(emitter.attr(attr)), rtype)
    if cl.has_dict:
        struct_name = cl.struct_name(emitter.names)
        # __dict__ lives right after the struct and __weakref__ lives right after that
        emitter.emit_gc_clear('*((PyObject **)((char *)self + sizeof({})))'.format(
            struct_name), object_rprimitive)
        emitter.emit_gc_clear(
            '*((PyObject **)((char *)self + sizeof(PyObject *) + sizeof({})))'.format(
                struct_name),
            object_rprimitive)
    emitter.emit_line('return 0;')
    emitter.emit_line('}')


def generate_dealloc_for_class(cl: ClassIR,
                               dealloc_func_name: str,
                               clear_func_name: str,
                               emitter: Emitter) -> None:
    emitter.emit_line('static void')
    emitter.emit_line('{}({} *self)'.format(dealloc_func_name, cl.struct_name(emitter.names)))
    emitter.emit_line('{')
    emitter.emit_line('PyObject_GC_UnTrack(self);')
    emitter.emit_line('{}(self);'.format(clear_func_name))
    emitter.emit_line('Py_TYPE(self)->tp_free((PyObject *)self);')
    emitter.emit_line('}')


def generate_methods_table(cl: ClassIR,
                           name: str,
                           emitter: Emitter) -> None:
    emitter.emit_line('static PyMethodDef {}[] = {{'.format(name))
    for fn in cl.methods.values():
        if fn.decl.is_prop_setter or fn.decl.is_prop_getter:
            continue
        emitter.emit_line('{{"{}",'.format(fn.name))
        emitter.emit_line(' (PyCFunction){}{},'.format(PREFIX, fn.cname(emitter.names)))
        flags = ['METH_VARARGS', 'METH_KEYWORDS']
        if fn.decl.kind == FUNC_STATICMETHOD:
            flags.append('METH_STATIC')
        elif fn.decl.kind == FUNC_CLASSMETHOD:
            flags.append('METH_CLASS')

        emitter.emit_line(' {}, NULL}},'.format(' | '.join(flags)))
    emitter.emit_line('{NULL}  /* Sentinel */')
    emitter.emit_line('};')


def generate_side_table_for_class(cl: ClassIR,
                                  name: str,
                                  type: str,
                                  slots: Dict[str, str],
                                  emitter: Emitter) -> Optional[str]:
    name = '{}_{}'.format(cl.name_prefix(emitter.names), name)
    emitter.emit_line('static {} {} = {{'.format(type, name))
    for field, value in slots.items():
        emitter.emit_line(".{} = {},".format(field, value))
    emitter.emit_line("};")
    return name


def generate_getseter_declarations(cl: ClassIR, emitter: Emitter) -> None:
    if not cl.is_trait:
        for attr in cl.attributes:
            emitter.emit_line('static PyObject *')
            emitter.emit_line('{}({} *self, void *closure);'.format(
                getter_name(cl, attr, emitter.names),
                cl.struct_name(emitter.names)))
            emitter.emit_line('static int')
            emitter.emit_line('{}({} *self, PyObject *value, void *closure);'.format(
                setter_name(cl, attr, emitter.names),
                cl.struct_name(emitter.names)))

    for prop in cl.properties:
        # Generate getter declaration
        emitter.emit_line('static PyObject *')
        emitter.emit_line('{}({} *self, void *closure);'.format(
            getter_name(cl, prop, emitter.names),
            cl.struct_name(emitter.names)))

        # Generate property setter declaration if a setter exists
        if cl.properties[prop][1]:
            emitter.emit_line('static int')
            emitter.emit_line('{}({} *self, PyObject *value, void *closure);'.format(
                setter_name(cl, prop, emitter.names),
                cl.struct_name(emitter.names)))


def generate_getseters_table(cl: ClassIR,
                             name: str,
                             emitter: Emitter) -> None:
    emitter.emit_line('static PyGetSetDef {}[] = {{'.format(name))
    if not cl.is_trait:
        for attr in cl.attributes:
            emitter.emit_line('{{"{}",'.format(attr))
            emitter.emit_line(' (getter){}, (setter){},'.format(
                getter_name(cl, attr, emitter.names), setter_name(cl, attr, emitter.names)))
            emitter.emit_line(' NULL, NULL},')
    for prop in cl.properties:
        emitter.emit_line('{{"{}",'.format(prop))
        emitter.emit_line(' (getter){},'.format(getter_name(cl, prop, emitter.names)))

        setter = cl.properties[prop][1]
        if setter:
            emitter.emit_line(' (setter){},'.format(setter_name(cl, prop, emitter.names)))
            emitter.emit_line('NULL, NULL},')
        else:
            emitter.emit_line('NULL, NULL, NULL},')

    emitter.emit_line('{NULL}  /* Sentinel */')
    emitter.emit_line('};')


def generate_getseters(cl: ClassIR, emitter: Emitter) -> None:
    if not cl.is_trait:
        for i, (attr, rtype) in enumerate(cl.attributes.items()):
            generate_getter(cl, attr, rtype, emitter)
            emitter.emit_line('')
            generate_setter(cl, attr, rtype, emitter)
            if i < len(cl.attributes) - 1:
                emitter.emit_line('')
    for prop, (getter, setter) in cl.properties.items():
        rtype = getter.sig.ret_type
        emitter.emit_line('')
        generate_readonly_getter(cl, prop, rtype, getter, emitter)
        if setter:
            arg_type = setter.sig.args[1].type
            emitter.emit_line('')
            generate_property_setter(cl, prop, arg_type, setter, emitter)


def generate_getter(cl: ClassIR,
                    attr: str,
                    rtype: RType,
                    emitter: Emitter) -> None:
    attr_field = emitter.attr(attr)
    emitter.emit_line('static PyObject *')
    emitter.emit_line('{}({} *self, void *closure)'.format(getter_name(cl, attr, emitter.names),
                                                           cl.struct_name(emitter.names)))
    emitter.emit_line('{')
    emit_undefined_check(rtype, emitter, attr_field, '==')
    emitter.emit_line('PyErr_SetString(PyExc_AttributeError,')
    emitter.emit_line('    "attribute {} of {} undefined");'.format(repr(attr),
                                                                    repr(cl.name)))
    emitter.emit_line('return NULL;')
    emitter.emit_line('}')
    emitter.emit_inc_ref('self->{}'.format(attr_field), rtype)
    emitter.emit_box('self->{}'.format(attr_field), 'retval', rtype, declare_dest=True)
    emitter.emit_line('return retval;')
    emitter.emit_line('}')


def generate_setter(cl: ClassIR,
                    attr: str,
                    rtype: RType,
                    emitter: Emitter) -> None:
    attr_field = emitter.attr(attr)
    emitter.emit_line('static int')
    emitter.emit_line('{}({} *self, PyObject *value, void *closure)'.format(
        setter_name(cl, attr, emitter.names),
        cl.struct_name(emitter.names)))
    emitter.emit_line('{')
    if rtype.is_refcounted:
        emit_undefined_check(rtype, emitter, attr_field, '!=')
        emitter.emit_dec_ref('self->{}'.format(attr_field), rtype)
        emitter.emit_line('}')
    emitter.emit_line('if (value != NULL) {')
    if rtype.is_unboxed:
        emitter.emit_unbox('value', 'tmp', rtype, custom_failure='return -1;', declare_dest=True)
    elif is_same_type(rtype, object_rprimitive):
        emitter.emit_line('PyObject *tmp = value;')
    else:
        emitter.emit_cast('value', 'tmp', rtype, declare_dest=True)
        emitter.emit_lines('if (!tmp)',
                           '    return -1;')
    emitter.emit_inc_ref('tmp', rtype)
    emitter.emit_line('self->{} = tmp;'.format(attr_field))
    emitter.emit_line('} else')
    emitter.emit_line('    self->{} = {};'.format(attr_field, emitter.c_undefined_value(rtype)))
    emitter.emit_line('return 0;')
    emitter.emit_line('}')


def generate_readonly_getter(cl: ClassIR,
                             attr: str,
                             rtype: RType,
                             func_ir: FuncIR,
                             emitter: Emitter) -> None:
    emitter.emit_line('static PyObject *')
    emitter.emit_line('{}({} *self, void *closure)'.format(getter_name(cl, attr, emitter.names),
                                                           cl.struct_name(emitter.names)))
    emitter.emit_line('{')
    if rtype.is_unboxed:
        emitter.emit_line('{}retval = {}{}((PyObject *) self);'.format(
            emitter.ctype_spaced(rtype), NATIVE_PREFIX, func_ir.cname(emitter.names)))
        emitter.emit_box('retval', 'retbox', rtype, declare_dest=True)
        emitter.emit_line('return retbox;')
    else:
        emitter.emit_line('return {}{}((PyObject *) self);'.format(NATIVE_PREFIX,
                                                                   func_ir.cname(emitter.names)))
    emitter.emit_line('}')


def generate_property_setter(cl: ClassIR,
                             attr: str,
                             arg_type: RType,
                             func_ir: FuncIR,
                             emitter: Emitter) -> None:

    emitter.emit_line('static int')
    emitter.emit_line('{}({} *self, PyObject *value, void *closure)'.format(
        setter_name(cl, attr, emitter.names),
        cl.struct_name(emitter.names)))
    emitter.emit_line('{')
    if arg_type.is_unboxed:
        emitter.emit_unbox('value', 'tmp', arg_type, custom_failure='return -1;',
                           declare_dest=True)
        emitter.emit_line('{}{}((PyObject *) self, tmp);'.format(
                          NATIVE_PREFIX,
                          func_ir.cname(emitter.names)))
    else:
        emitter.emit_line('{}{}((PyObject *) self, value);'.format(
                          NATIVE_PREFIX,
                          func_ir.cname(emitter.names)))
    emitter.emit_line('return 0;')
    emitter.emit_line('}')


def emit_undefined_check(rtype: RType, emitter: Emitter, attr: str, compare: str) -> None:
    if isinstance(rtype, RTuple):
        attr_expr = 'self->{}'.format(attr)
        emitter.emit_line(
            'if ({}) {{'.format(
                emitter.tuple_undefined_check_cond(
                    rtype, attr_expr, emitter.c_undefined_value, compare)))
    else:
        emitter.emit_line(
            'if (self->{} {} {}) {{'.format(attr, compare, emitter.c_undefined_value(rtype)))
