import sys
import operator
from collections import OrderedDict
from functools import reduce

from django.db import models
from django.db.models.fields.related import ForeignObjectRel
from django.db.models.constants import LOOKUP_SEP
from django.db.models.sql.constants import QUERY_TERMS
from django.template.response import TemplateResponse
from django.http import HttpResponseRedirect
from django.forms import Form, Media, ModelChoiceField

from django.core.urlresolvers import reverse, NoReverseMatch
from django.core.exceptions import (
    FieldDoesNotExist, ImproperlyConfigured, SuspiciousOperation,
)
from django.core.paginator import Paginator, InvalidPage

from django.contrib.auth import get_permission_codename
from django.contrib.admin import FieldListFilter, widgets
from django.contrib.admin.options import IncorrectLookupParameters
from django.contrib.admin.exceptions import DisallowedModelAdminLookup
from django.contrib.admin.utils import (
    get_fields_from_path, lookup_needs_distinct, prepare_lookup_value, quote)

from django.utils import six
from django.utils.translation import ugettext as _
from django.utils.encoding import force_text
from django.utils.http import urlencode
from django.utils.functional import cached_property
from django.views.generic import View

from wagtail.wagtailcore.models import Page

# ListView settings
ORDER_VAR = 'o'
ORDER_TYPE_VAR = 'ot'
PAGE_VAR = 'p'
SEARCH_VAR = 'q'
ERROR_FLAG = 'e'

IGNORED_PARAMS = (ORDER_VAR, ORDER_TYPE_VAR, SEARCH_VAR)


class ModelAdminView(View):
    def __init__(self, request, model, modeladmin=None):
        self.request = request
        self.model = model
        self.opts = model._meta
        self.modeladmin = modeladmin
        self.is_pagemodel = issubclass(self.model, Page)

    @cached_property
    def app_label(self):
        return force_text(self.opts.app_label)

    @cached_property
    def model_name(self):
        return force_text(self.opts.verbose_name)

    @cached_property
    def model_name_plural(self):
        return force_text(self.opts.verbose_name_plural)

    def try_for_modeladmin_attr(self, attr_name, fallback=None):
        if self.modeladmin:
            return getattr(self.modeladmin, attr_name, fallback)
        return fallback

    def try_for_modeladmin_method(self, method_name, fallback, *args, **kwargs):
        if self.modeladmin:
            method = getattr(self.modeladmin, method_name, None)
            if method is None:
                return fallback
            return method(*args, **kwargs)
        return fallback

    def get_menu_icon(self):
        return self.try_for_modeladmin_method('get_menu_icon',
                                              'icon-doc-full-inverse')

    def get_context_data(self, request, *args, **kwargs):
        if self.modeladmin and self.modeladmin.parent:
            app_label = self.modeladmin.parent.get_menu_label().lower()
        else:
            app_label = self.app_label
        context = {
            'app_label': app_label,
            'module_name': self.model_name,
            'module_name_plural': self.model_name_plural,
            'module_icon': self.get_menu_icon(),
            'is_pagemodel': self.is_pagemodel,
            'list_url': self.try_for_modeladmin_method('get_list_url', ''),
            'choose_parent_url': self.try_for_modeladmin_method('get_choose_parent_url', ''),
            'add_url': self.try_for_modeladmin_method('get_add_url', ''),
        }
        context.update(
            self.try_for_modeladmin_method('get_context_data', {}, request)
        )
        return context

    def has_add_permission(self, request):
        if self.modeladmin:
            return self.modeladmin.has_add_permission(request)
        if self.is_pagemodel:
            return bool(self.get_valid_parent_pages(request))
        opts = self.opts
        codename = get_permission_codename('add', opts)
        return request.user.has_perm("%s.%s" % (opts.app_label, codename))

    def permissions_for_user(self, user, obj):
        if self.is_pagemodel:
            return obj.permissions_for_user(user)
        opts = self.opts
        app_label = opts.app_label
        return {
            'can_edit': user.has_perm("%s.%s" % (
                app_label, get_permission_codename('change', self.opts))),
            'can_delete': user.has_perm("%s.%s" % (
                app_label, get_permission_codename('delete', self.opts))),
        }

    def edit_button(self, obj):
        pk = getattr(obj, self.pk_attname)
        model_name = self.model_name.lower()
        if self.is_pagemodel:
            url = reverse('wagtailadmin_pages_edit', args=(quote(pk),))
        else:
            url_args = (self.opts.app_label, self.opts.model_name, quote(pk))
            url = reverse('wagtailsnippets_edit', args=url_args,)
        return {
            'title': _('Edit this %(mn)s') % {'mn': model_name},
            'label': _('Edit'),
            'url': url,
        }

    def delete_button(self, obj):
        pk = getattr(obj, self.pk_attname)
        model_name = self.model_name.lower()
        if self.is_pagemodel:
            url = reverse('wagtailadmin_pages_delete', args=(quote(pk),))
        else:
            url_args = (self.opts.app_label, self.opts.model_name, quote(pk))
            url = reverse('wagtailsnippets_delete', args=url_args,)
        return {
            'title': _('Delete this %(mn)s') % {'mn': model_name},
            'label': _('Delete'),
            'url': url,
        }

    def unpublish_button(self, obj):
        pk = getattr(obj, self.pk_attname)
        model_name = self.model_name.lower()
        url = reverse('wagtailadmin_pages_unpublish', args=(quote(pk),))
        return {
            'title': _('Unpublish this %(mn)s') % {'mn': model_name},
            'label': _('Unpublish'),
            'url': url,
        }

    def get_action_buttons_for_obj(self, user, obj):
        buttons = []
        perms = self.permissions_for_user(user, obj)
        if self.is_pagemodel:
            if perms.can_edit():
                buttons.append(self.edit_button(obj))
            if perms.can_unpublish() and obj.live:
                buttons.append(self.unpublish_button(obj))
            if perms.can_delete():
                buttons.append(self.delete_button(obj))
        else:
            if perms['can_edit']:
                buttons.append(self.edit_button(obj))
            if perms['can_delete']:
                buttons.append(self.delete_button(obj))
        return buttons


class ListView(ModelAdminView):
    def __init__(self, request, model, modeladmin=None, list_display=None,
                 list_filter=None, search_fields=None, items_per_page=None,
                 select_related=None):
        super(ListView, self).__init__(request, model, modeladmin)

        self.list_display = list_display or self.try_for_modeladmin_method(
            'get_list_display', ['__str__'], request)

        self.list_filter = list_filter or self.try_for_modeladmin_method(
            'get_list_display', [], request)

        self.search_fields = search_fields or self.try_for_modeladmin_method(
            'get_search_fields', [], request)

        self.items_per_page = items_per_page or self.try_for_modeladmin_attr(
            'list_per_page', 100)

        self.select_related = select_related or self.try_for_modeladmin_attr(
            'list_select_related', False)

        # Get search parameters from the query string.
        try:
            self.page_num = int(request.GET.get(PAGE_VAR, 0))
        except ValueError:
            self.page_num = 0

        self.params = dict(request.GET.items())
        if PAGE_VAR in self.params:
            del self.params[PAGE_VAR]
        if ERROR_FLAG in self.params:
            del self.params[ERROR_FLAG]

        self.query = request.GET.get(SEARCH_VAR, '')
        self.pk_attname = self.opts.pk.attname

    def url_for_result(self, result):
        raise NoReverseMatch

    def get_search_results(self, request, queryset, search_term):
        """
        Returns a tuple containing a queryset to implement the search,
        and a boolean indicating if the results may contain duplicates.
        """
        # Apply keyword searches.
        def construct_search(field_name):
            if field_name.startswith('^'):
                return "%s__istartswith" % field_name[1:]
            elif field_name.startswith('='):
                return "%s__iexact" % field_name[1:]
            elif field_name.startswith('@'):
                return "%s__search" % field_name[1:]
            else:
                return "%s__icontains" % field_name

        use_distinct = False
        if self.search_fields and search_term:
            orm_lookups = [construct_search(str(search_field))
                           for search_field in self.search_fields]
            for bit in search_term.split():
                or_queries = [models.Q(**{orm_lookup: bit})
                              for orm_lookup in orm_lookups]
                queryset = queryset.filter(reduce(operator.or_, or_queries))
            if not use_distinct:
                for search_spec in orm_lookups:
                    if lookup_needs_distinct(self.opts, search_spec):
                        use_distinct = True
                        break

        return queryset, use_distinct

    def get_base_queryset(self, request):
        """
        Returns a QuerySet of all model instances that can be edited by the
        admin site.
        """
        qs = self.model._default_manager.get_queryset()
        ordering = self.get_ordering(request)
        if ordering:
            qs = qs.order_by(*ordering)
        return qs

    def get_paginator(self, request, queryset, per_page):
        return Paginator(queryset, per_page)

    def lookup_allowed(self, lookup, value):
        # Check FKey lookups that are allowed, so that popups produced by
        # ForeignKeyRawIdWidget, on the basis of ForeignKey.limit_choices_to,
        # are allowed to work.
        for l in self.model._meta.related_fkey_lookups:
            for k, v in widgets.url_params_from_lookup_dict(l).items():
                if k == lookup and v == value:
                    return True

        parts = lookup.split(LOOKUP_SEP)

        # Last term in lookup is a query term (__exact, __startswith etc)
        # This term can be ignored.
        if len(parts) > 1 and parts[-1] in QUERY_TERMS:
            parts.pop()

        # Special case -- foo__id__exact and foo__id queries are implied
        # if foo has been specifically included in the lookup list; so
        # drop __id if it is the last part. However, first we need to find
        # the pk attribute name.
        rel_name = None
        for part in parts[:-1]:
            try:
                field, _, _, _ = self.model._meta.get_field_by_name(part)
            except FieldDoesNotExist:
                # Lookups on non-existent fields are ok, since they're ignored
                # later.
                return True
            if hasattr(field, 'rel'):
                if field.rel is None:
                    # This property or relation doesn't exist, but it's allowed
                    # since it's ignored in ChangeList.get_filters().
                    return True
                model = field.rel.to
                rel_name = field.rel.get_related_field().name
            elif isinstance(field, ForeignObjectRel):
                model = field.model
                rel_name = model._meta.pk.name
            else:
                rel_name = None
        if rel_name and len(parts) > 1 and parts[-1] == rel_name:
            parts.pop()

        if len(parts) == 1:
            return True
        clean_lookup = LOOKUP_SEP.join(parts)
        return clean_lookup in self.list_filter

    def get_filters_params(self, params=None):
        """
        Returns all params except IGNORED_PARAMS
        """
        if not params:
            params = self.params
        lookup_params = params.copy()  # a dictionary of the query string
        # Remove all the parameters that are globally and systematically
        # ignored.
        for ignored in IGNORED_PARAMS:
            if ignored in lookup_params:
                del lookup_params[ignored]
        return lookup_params

    def get_filters(self, request):
        lookup_params = self.get_filters_params()
        use_distinct = False

        for key, value in lookup_params.items():
            if not self.lookup_allowed(key, value):
                raise DisallowedModelAdminLookup(
                    "Filtering by %s not allowed" % key)

        filter_specs = []
        if self.list_filter:
            for list_filter in self.list_filter:
                if callable(list_filter):
                    # This is simply a custom list filter class.
                    spec = list_filter(
                        request,
                        lookup_params,
                        self.model,
                        self.modeladmin)
                else:
                    field_path = None
                    if isinstance(list_filter, (tuple, list)):
                        # This is a custom FieldListFilter class for a given
                        # field.
                        field, field_list_filter_class = list_filter
                    else:
                        # This is simply a field name, so use the default
                        # FieldListFilter class that has been registered for
                        # the type of the given field.
                        field = list_filter
                        field_list_filter_class = FieldListFilter.create
                    if not isinstance(field, models.Field):
                        field_path = field
                        field = get_fields_from_path(self.model,
                                                     field_path)[-1]
                    spec = field_list_filter_class(
                        field,
                        request,
                        lookup_params,
                        self.model,
                        self.modeladmin,
                        field_path=field_path)

                    # Check if we need to use distinct()
                    use_distinct = (
                        use_distinct or lookup_needs_distinct(self.opts,
                                                              field_path))
                if spec and spec.has_output():
                    filter_specs.append(spec)

        # At this point, all the parameters used by the various ListFilters
        # have been removed from lookup_params, which now only contains other
        # parameters passed via the query string. We now loop through the
        # remaining parameters both to ensure that all the parameters are valid
        # fields and to determine if at least one of them needs distinct(). If
        # the lookup parameters aren't real fields, then bail out.
        try:
            for key, value in lookup_params.items():
                lookup_params[key] = prepare_lookup_value(key, value)
                use_distinct = (
                    use_distinct or lookup_needs_distinct(self.opts, key))
            return (
                filter_specs, bool(filter_specs), lookup_params, use_distinct
            )
        except FieldDoesNotExist as e:
            six.reraise(
                IncorrectLookupParameters,
                IncorrectLookupParameters(e),
                sys.exc_info()[2])

    def get_query_string(self, new_params=None, remove=None):
        if new_params is None:
            new_params = {}
        if remove is None:
            remove = []
        p = self.params.copy()
        for r in remove:
            for k in list(p):
                if k.startswith(r):
                    del p[k]
        for k, v in new_params.items():
            if v is None:
                if k in p:
                    del p[k]
            else:
                p[k] = v
        return '?%s' % urlencode(sorted(p.items()))

    def get_default_ordering(self, request):
        if self.modeladmin and self.modeladmin.get_ordering(request):
            return self.modeladmin.get_ordering(request)
        if self.opts.ordering:
            return self.opts.ordering
        return []

    def get_ordering_field(self, field_name):
        """
        Returns the proper model field name corresponding to the given
        field_name to use for ordering. field_name may either be the name of a
        proper model field or the name of a method (on the admin or model) or a
        callable with the 'admin_order_field' attribute. Returns None if no
        proper model field name can be matched.
        """
        try:
            field = self.opts.get_field(field_name)
            return field.name
        except FieldDoesNotExist:
            # See whether field_name is a name of a non-field
            # that allows sorting.
            if callable(field_name):
                attr = field_name
            elif hasattr(self.modeladmin, field_name):
                attr = getattr(self.modeladmin, field_name)
            else:
                attr = getattr(self.model, field_name)
            return getattr(attr, 'admin_order_field', None)

    def get_ordering(self, request, queryset):
        """
        Returns the list of ordering fields for the change list.
        First we check the get_ordering() method in model admin, then we check
        the object's default ordering. Then, any manually-specified ordering
        from the query string overrides anything. Finally, a deterministic
        order is guaranteed by ensuring the primary key is used as the last
        ordering field.
        """
        params = self.params
        ordering = self.get_default_ordering(request)
        if ORDER_VAR in params:
            # Clear ordering and used params
            ordering = []
            order_params = params[ORDER_VAR].split('.')
            for p in order_params:
                try:
                    none, pfx, idx = p.rpartition('-')
                    field_name = self.list_display[int(idx)]
                    order_field = self.get_ordering_field(field_name)
                    if not order_field:
                        continue  # No 'admin_order_field', skip it
                    # reverse order if order_field has already "-" as prefix
                    if order_field.startswith('-') and pfx == "-":
                        ordering.append(order_field[1:])
                    else:
                        ordering.append(pfx + order_field)
                except (IndexError, ValueError):
                    continue  # Invalid ordering specified, skip it.

        # Add the given query's ordering fields, if any.
        ordering.extend(queryset.query.order_by)

        # Ensure that the primary key is systematically present in the list of
        # ordering fields so we can guarantee a deterministic order across all
        # database backends.
        pk_name = self.opts.pk.name
        if not (set(ordering) & {'pk', '-pk', pk_name, '-' + pk_name}):
            # The two sets do not intersect, meaning the pk isn't present. So
            # we add it.
            ordering.append('-pk')

        return ordering

    def get_ordering_field_columns(self):
        """
        Returns an OrderedDict of ordering field column numbers and asc/desc
        """

        # We must cope with more than one column having the same underlying
        # sort field, so we base things on column numbers.
        ordering = self._get_default_ordering()
        ordering_fields = OrderedDict()
        if ORDER_VAR not in self.params:
            # for ordering specified on ModelAdmin or model Meta, we don't know
            # the right column numbers absolutely, because there might be more
            # than one column associated with that ordering, so we guess.
            for field in ordering:
                if field.startswith('-'):
                    field = field[1:]
                    order_type = 'desc'
                else:
                    order_type = 'asc'
                for index, attr in enumerate(self.list_display):
                    if self.get_ordering_field(attr) == field:
                        ordering_fields[index] = order_type
                        break
        else:
            for p in self.params[ORDER_VAR].split('.'):
                none, pfx, idx = p.rpartition('-')
                try:
                    idx = int(idx)
                except ValueError:
                    continue  # skip it
                ordering_fields[idx] = 'desc' if pfx == '-' else 'asc'
        return ordering_fields

    def get_queryset(self, request):
        # First, we collect all the declared list filters.
        (self.filter_specs, self.has_filters, remaining_lookup_params,
         filters_use_distinct) = self.get_filters(request)

        # Then, we let every list filter modify the queryset to its liking.
        qs = self.get_base_queryset()
        for filter_spec in self.filter_specs:
            new_qs = filter_spec.queryset(request, qs)
            if new_qs is not None:
                qs = new_qs

        try:
            # Finally, we apply the remaining lookup parameters from the query
            # string (i.e. those that haven't already been processed by the
            # filters).
            qs = qs.filter(**remaining_lookup_params)
        except (SuspiciousOperation, ImproperlyConfigured):
            # Allow certain types of errors to be re-raised as-is so that the
            # caller can treat them in a special way.
            raise
        except Exception as e:
            # Every other error is caught with a naked except, because we don't
            # have any other way of validating lookup parameters. They might be
            # invalid if the keyword arguments are incorrect, or if the values
            # are not in the correct type, so we might get FieldError,
            # ValueError, ValidationError, or ?.
            raise IncorrectLookupParameters(e)

        if not qs.query.select_related:
            qs = self.apply_select_related(qs)

        # Set ordering.
        ordering = self.get_ordering(request, qs)
        qs = qs.order_by(*ordering)

        # Apply search results
        qs, search_use_distinct = self.get_search_results(
            request, qs, self.query)

        # Remove duplicates from results, if necessary
        if filters_use_distinct | search_use_distinct:
            return qs.distinct()
        else:
            return qs

    def apply_select_related(self, qs):
        if self.select_related is True:
            return qs.select_related()

        if self.select_related is False:
            if self.has_related_field_in_list_display():
                return qs.select_related()

        if self.select_related:
            return qs.select_related(*self.select_related)
        return qs

    def has_related_field_in_list_display(self):
        for field_name in self.list_display:
            try:
                field = self.opts.get_field(field_name)
            except FieldDoesNotExist:
                pass
            else:
                if isinstance(field.remote_field, models.ManyToOneRel):
                    return True
        return False

    def get_context_data(self, request, *args, **kwargs):
        context = super(ListView, self).get_context_data()
        context.update(
            self.try_for_modeladmin_method('get_list_view_context_data', {},
                                           request))
        return context

    def get_extra_media(self, request):
        return self.try_for_modeladmin_method('get_list_view_media', Media(),
                                              request)

    def get(self, request, *args, **kwargs):
        context = self.get_context_data(request, *args, **kwargs)

        queryset = self.get_queryset(request)
        paginator = self.get_paginator(request, queryset, self.items_per_page)
        total_count = paginator.count
        multi_page = total_count > self.items_per_page

        # Get the list of objects to display on this page.
        try:
            page_obj = paginator.page(self.page_num + 1)
        except InvalidPage:
            page_obj = paginator.page(1)
        result_list = page_obj.object_list

        context.update({
            'total_count': total_count,
            'multi_page': multi_page,
            'page_obj': page_obj,
            'result_list': result_list,
            'paginator': paginator,
            'selection_note': _('0 of %(cnt)s selected') % {
                'cnt': len(result_list)},
            'title': _('Select a %s to change') % self.model_name,
            'no_items': bool(not self.model.objects.all().count()),
            'cl': self,
            'has_add_permission': self.has_add_permission(request),
            'media': self.get_extra_media(request),
        })

        if self.is_pagemodel:
            valid_parent_count = self.get_valid_parent_pages(request).count()
            context.update({
                'no_valid_parents': not valid_parent_count,
                'required_parent_types': self.model.allowed_parent_page_types(),
            })

        return TemplateResponse(request, self.get_template(), context)

    def get_template(self):
        opts = self.opts
        return [
            'wagtailmodeladmin/%s/%s/change_list.html' % (opts.app_label,
                                                          opts.model_name),
            'wagtailmodeladmin/%s/change_list.html' % opts.app_label,
            'wagtailmodeladmin/change_list.html',
        ]


class AddView(ModelAdminView):
    pass


class EditView(ModelAdminView):
    pass


class ChooseParentView(ModelAdminView):
    pass
