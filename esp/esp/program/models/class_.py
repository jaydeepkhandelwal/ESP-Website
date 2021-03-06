__author__    = "Individual contributors (see AUTHORS file)"
__date__      = "$DATE$"
__rev__       = "$REV$"
__license__   = "AGPL v.3"
__copyright__ = """
This file is part of the ESP Web Site
Copyright (c) 2008 by the individual contributors
  (see AUTHORS file)

The ESP Web Site is free software; you can redistribute it and/or
modify it under the terms of the GNU Affero General Public License
as published by the Free Software Foundation; either version 3
of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public
License along with this program; if not, write to the Free Software
Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.

Contact information:
MIT Educational Studies Program
  84 Massachusetts Ave W20-467, Cambridge, MA 02139
  Phone: 617-253-4882
  Email: esp-webmasters@mit.edu
Learning Unlimited, Inc.
  527 Franklin St, Cambridge, MA 02139
  Phone: 617-379-0178
  Email: web-team@lists.learningu.org
"""

import datetime
import time
from collections import defaultdict

# django Util
from django.db import models
from django.db.models import get_model
from django.db.models.query import Q
from django.db.models import signals
from django.core.cache import cache
from django.utils.datastructures import SortedDict
from django.template.loader import render_to_string
from django.template import Template, Context

# ESP Util
from esp.db.models.prepared import ProcedureManager
from esp.db.fields import AjaxForeignKey
from esp.db.cache import GenericCacheHelper
from esp.utils.property import PropertyDict
from esp.utils.fields import JSONField
from esp.tagdict.models import Tag
from esp.mailman import add_list_member, remove_list_member

# django models
from django.contrib.auth.models import User

# ESP models
from esp.miniblog.models import Entry
from esp.datatree.models import *
from esp.cal.models import Event
from esp.qsd.models import QuasiStaticData
from esp.qsdmedia.models import Media as QSDMedia
from esp.users.models import ESPUser, UserBit, UserAvailability
from esp.middleware              import ESPError
from esp.program.models          import Program, StudentRegistration, RegistrationType
from esp.program.models import BooleanExpression, ScheduleMap, ScheduleConstraint, ScheduleTestOccupied, ScheduleTestCategory, ScheduleTestSectionList
from esp.resources.models        import ResourceType, Resource, ResourceRequest, ResourceAssignment
from esp.cache                   import cache_function
from esp.utils.derivedfield      import DerivedField

from django.core.cache import cache  ## Yep, we do have to do some raw cache-management for performance.  Try to minimize it, though.
#from pylibmc import NotFound as CacheNotFound

from esp.middleware.threadlocalrequest import get_current_request

from esp.customforms.linkfields import CustomFormsLinkModel

__all__ = ['ClassSection', 'ClassSubject', 'ProgramCheckItem', 'ClassManager', 'ClassCategories', 'ClassImplication', 'ClassSizeRange']

class ClassSizeRange(models.Model):
    from esp.program.models import Program

    range_min = models.IntegerField(null=False)
    range_max = models.IntegerField(null=False)
    program   = models.ForeignKey(Program, blank=True, null=True)

    @classmethod
    def get_ranges_for_program(cls, prog):
        ranges = cls.objects.filter(program=prog)
        if ranges:
            return ranges
        else:
            for range in cls.objects.filter(program=None):
                k = cls()
                k.range_min = range.range_min
                k.range_max = range.range_max
                k.program = prog
                k.save()
            return cls.objects.filter(program=prog)

    def range_str(self):
        return "%d-%d" %(self.range_min, self.range_max)

    def __unicode__(self):
        return "Class Size Range: " + self.range_str()

    class Meta:
        app_label='program'

class ProgramCheckItem(models.Model):
    from esp.program.models import Program
    
    program = models.ForeignKey(Program, related_name='checkitems')
    title   = models.CharField(max_length=512)
    seq     = models.PositiveIntegerField(blank=True,verbose_name='Sequence',
                                          help_text = 'Lower is earlier')

    def save(self, *args, **kwargs):
        if self.seq is None:
            try:
                item = ProgramCheckItem.objects.filter(program = self.program).order_by('-seq')[0]
                self.seq = item.seq + 5
            except IndexError:
                self.seq = 0
        super(ProgramCheckItem, self).save(*args, **kwargs)

    def __unicode__(self):
        return '%s for "%s"' % (self.title, str(self.program).strip())

    class Meta:
        ordering = ('seq',)
        app_label = 'program'
        db_table = 'program_programcheckitem'


class ClassCacheHelper(GenericCacheHelper):
    @staticmethod
    def get_key(cls):
        return 'ClassCache__%s' % cls._get_pk_val()
    
class SectionCacheHelper(GenericCacheHelper):
    @staticmethod
    def get_key(cls):
        return 'SectionCache__%s' % cls._get_pk_val()

class ClassManager(ProcedureManager):
    def __repr__(self):
        return "ClassManager()"
    
    def approved(self, return_q_obj=False):
        if return_q_obj:
            return Q(status = 10)
        
        return self.filter(status = 10)

    def catalog(self, program, ts=None, force_all=False, initial_queryset=None, use_cache=True, cache_only=False, order_args_override=None):
        # Try getting the catalog straight from cache
        catalog = self.catalog_cached(program, ts, force_all, initial_queryset, cache_only=True, order_args_override=order_args_override)
        if catalog is None:
            # Get it from the DB, then try prefetching class sizes
            catalog = self.catalog_cached(program, ts, force_all, initial_queryset, use_cache=use_cache, cache_only=cache_only, order_args_override=order_args_override)
        else:
            for cls in catalog:
                for sec in cls.get_sections():
                    if hasattr(sec, '_count_students'):
                        del sec._count_students

        return catalog

    
    @cache_function
    def catalog_cached(self, program, ts=None, force_all=False, initial_queryset=None, order_args_override=None):
        """ Return a queryset of classes for view in the catalog.

        In addition to just giving you the classes, it also
        queries for the category's title (cls.category_txt)
        and the total # of media.
        """
        now = datetime.datetime.now()
        
        enrolled_type = RegistrationType.get_map(include=['Enrolled'], category='student')['Enrolled']
        teaching_node=GetNode("V/Flags/Registration/Teacher")

        if initial_queryset:
            classes = initial_queryset
        else:
            classes = self.all()
        
        if not force_all:
            classes = classes.filter(self.approved(return_q_obj=True))
        
        classes = classes.select_related('anchor',
                                         'category')
        
        if program != None:
            classes = classes.filter(parent_program = program)

        if ts is not None:
            classes = classes.filter(sections__meeting_times=ts)
        
        select = SortedDict([( '_num_students', 'SELECT COUNT(DISTINCT "program_studentregistration"."user_id") FROM "program_studentregistration", "program_classsection" WHERE ("program_studentregistration"."relationship_id" = %s AND "program_studentregistration"."section_id" = "program_classsection"."id" AND "program_classsection"."parent_class_id" = "program_class"."id" AND "program_studentregistration"."start_date" <= %s AND "program_studentregistration"."end_date" >= %s)'),
                             ('teacher_ids', 'SELECT list(DISTINCT "users_userbit"."user_id") FROM "users_userbit" WHERE ("users_userbit"."verb_id" = %s AND "users_userbit"."qsc_id" = "program_class"."anchor_id" AND "users_userbit"."enddate" >= %s AND "users_userbit"."startdate" <= %s)'),
                             ('media_count', 'SELECT COUNT(*) FROM "qsdmedia_media" WHERE ("qsdmedia_media"."anchor_id" = "program_class"."anchor_id")'),
                             ('_index_qsd', 'SELECT list("qsd_quasistaticdata"."id") FROM "qsd_quasistaticdata" WHERE ("qsd_quasistaticdata"."path_id" = "program_class"."anchor_id" AND "qsd_quasistaticdata"."name" = \'learn:index\')'),
                             ('_studentapps_count', 'SELECT COUNT(*) FROM "program_studentappquestion" WHERE ("program_studentappquestion"."subject_id" = "program_class"."id")')])
                             
        select_params = [ enrolled_type.id,
                          now,
                          now,
                          teaching_node.id,
                          now,
                          now,
                         ]
        classes = classes.extra(select=select, select_params=select_params)
        
        #   Allow customized orderings for the catalog.
        #   These are the default ordering fields in descending order of priority.
        if order_args_override:
            order_args = order_args_override
        else:
            order_args = ['category__symbol', 'sections__meeting_times__start', '_num_students', 'id']
            #   First check if there is an ordering specified for the program.
            program_sort_fields = Tag.getProgramTag('catalog_sort_fields', program)
            if program_sort_fields:
                #   If you found one, use it.
                order_args = program_sort_fields.split(',')
        
        #   Order the QuerySet using the specified list.
        classes = classes.order_by(*order_args)
        
        classes = classes.distinct()
        classes = list(classes)
        
        #   Filter out duplicates by ID.  This is necessary because Django's ORM
        #   adds the related fields (e.g. sections__meeting_times) to the SQL
        #   SELECT statement and doesn't include them in the result.
        #   See http://docs.djangoproject.com/en/dev/ref/models/querysets/#s-distinct
        counter = 0
        index = 0
        max_count = len(classes)
        id_list = []
        while counter < max_count:
            cls = classes[index]
            cls._temp_index = counter
            if cls.id not in id_list:
                id_list.append(cls.id)
                index += 1
            else:
                classes.remove(cls)
            counter += 1

        # All class ID's; used by later query ugliness:
        class_ids = map(lambda x: x.id, classes)
        
        # Now to get the sections corresponding to these classes...

        sections = ClassSection.objects.filter(parent_class__in=class_ids)
        
        sections = sections.select_related('anchor')

        sections = ClassSection.prefetch_catalog_data(sections.distinct())

        sections_by_parent_id = defaultdict(list)
        for s in sections:
            sections_by_parent_id[s.parent_class_id].append(s)
        
        # We got classes.  Now get teachers...
        if program != None:
            teachers = ESPUser.objects.filter(userbit__verb=teaching_node, userbit__qsc__parent__parent=program.anchor_id, userbit__startdate__lte=now, userbit__enddate__gte=now).distinct()
        else:
            teachers = ESPUser.objects.filter(userbit__verb=teaching_node, userbit__startdate__lte=now, userbit__enddate__gte=now).distinct()

        teachers_by_id = {}
        for t in teachers:            
            teachers_by_id[t.id] = t

        # Now, to combine all of the above

        if len(classes) >= 1:
            p = Program.objects.select_related('anchor').get(id=classes[0].parent_program_id)
            
        for c in classes:
            c._teachers = [teachers_by_id[int(x)] for x in c.teacher_ids.split(',')] if c.teacher_ids != '' else []
            c._teachers.sort(cmp=lambda t1, t2: cmp(t1.last_name, t2.last_name))
            c._sections = sections_by_parent_id[c.id]
            for s in c._sections:
                s.parent_class = c
            c._sections.sort(cmp=lambda s1, s2: cmp(s1.anchor.name, s2.anchor.name))
            c.parent_program = p # So that if we set attributes on one instance of the program,
                                 # they show up for all instances.
            
        return classes
    catalog_cached.depend_on_model(lambda: ClassSubject)
    catalog_cached.depend_on_model(lambda: ClassSection)
    catalog_cached.depend_on_model(lambda: QSDMedia)
    catalog_cached.depend_on_model(lambda: Tag)
    catalog_cached.depend_on_row(lambda: UserBit, lambda bit: {},
                                 lambda bit: bit.applies_to_verb('V/Flags/Registration/Teacher'))
    #catalog_cached.depend_on_row(lambda: UserBit, lambda bit: {},
    #                             lambda bit: bit.applies_to_verb('V/Flags/Registration/Enrolled')) # This will expire a *lot*, and the value that it saves can be gotten from cache (with effort) instead of from SQL.  Should go do that.
    catalog_cached.depend_on_row(lambda: QuasiStaticData, lambda page: {},
                                 lambda page: ("learn:index" == page.name) and ("Q/Programs/" in page.path.get_uri()) and ("/Classes/" in page.path.get_uri())) # Slightly dirty hack; has assumptions about the tree structure of where index.html pages for QSD will be stored
    

    cache = ClassCacheHelper


def checklist_progress_base(class_name):
    """ The main Manage page requests checklist_progress.all() O(n) times
    per checkbox in the program.  Minimize the number of these calls that
    actually hit the db. """
    def _progress(self):
        CACHE_KEY = class_name.upper() + "__CHECKLIST_PROGRESS__CACHE__%d" % self.id
        val = cache.get(CACHE_KEY)
        if val == None:
            val = self.checklist_progress.all()
            len(val) # force the query to be executed before caching it
            cache.set(CACHE_KEY, val, 1)
    
        return val
    return _progress

class ClassSection(models.Model):
    """ An instance of class.  There should be one of these for each weekend of HSSP, for example; or multiple
    parallel sections for a course being taught more than once at Splash or Spark. """
    
    anchor = AjaxForeignKey(DataTree)
    status = models.IntegerField(default=0)                 #   -10 = rejected, 0 = unreviewed, 10 = accepted
    registration_status = models.IntegerField(default=0)    #   0 = open, 10 = closed
    duration = models.DecimalField(blank=True, null=True, max_digits=5, decimal_places=2)
    meeting_times = models.ManyToManyField(Event, related_name='meeting_times', blank=True)
    checklist_progress = models.ManyToManyField(ProgramCheckItem, blank=True)
    max_class_capacity = models.IntegerField(blank=True, null=True)
    
    cache = SectionCacheHelper
    checklist_progress_all_cached = checklist_progress_base('ClassSection')
    parent_class = AjaxForeignKey('ClassSubject', related_name='sections')

    registrations = models.ManyToManyField(ESPUser, through='StudentRegistration')

    @classmethod
    def ajax_autocomplete(cls, data):
        clsname = data.strip().split(':')
        id_ = clsname[0].split('s')
        sec_index = ''.join(id_[1:])
        id_ = id_[0]
        
        query_set = cls.objects.filter(parent_class__anchor__name__istartswith=id_.split('s')[0])

        if len(clsname) > 1:
            title  = ':'.join(clsname[1:])
            if len(title.strip()) > 0:
                query_set = query_set.filter(parent_class__anchor__friendly_name__istartswith = title.strip())

        values_set = query_set.order_by('anchor__name').select_related()#.values('anchor__name', 'parent_class__anchor__friendly_name', 'id')
        values = []
        for v in values_set:
            index = str(v.index())
            if (not sec_index) or (sec_index == index):
                values.append({'parent_class__anchor__name': v.parent_class.anchor.name,
                               'parent_class__anchor__friendly_name': v.parent_class.anchor.friendly_name,
                               'secnum': index,
                               'id': str(v.id)})

        for value in values:
            value['ajax_str'] = '%ss%s: %s' % (value['parent_class__anchor__name'], value['secnum'], value['parent_class__anchor__friendly_name'])
        return values
    
    @classmethod
    def prefetch_catalog_data(cls, queryset):
        """ Take a queryset of a set of ClassSubject's, and annotate each class in it with the '_count_students' and 'event_ids' fields (used internally when available by many functions to save on queries later) """
        now = datetime.datetime.now()
        enrolled_type = RegistrationType.get_map()['Enrolled']

        select = SortedDict([( '_count_students', 'SELECT COUNT(DISTINCT "program_studentregistration"."user_id") FROM "program_studentregistration" WHERE ("program_studentregistration"."relationship_id" = %s AND "program_studentregistration"."section_id" = "program_classsection"."id" AND "program_studentregistration"."start_date" <= %s AND "program_studentregistration"."end_date" >= %s)'),
                             ('event_ids', 'SELECT list("cal_event"."id") FROM "cal_event", "program_classsection_meeting_times" WHERE ("program_classsection_meeting_times"."event_id" = "cal_event"."id" AND "program_classsection_meeting_times"."classsection_id" = "program_classsection"."id")')])
        
        select_params = [ enrolled_type.id,
                          now,
                          now,
                         ]

        sections = queryset.extra(select=select, select_params=select_params)
        sections = list(sections)
        section_ids = map(lambda x: x.id, sections)

        # Now, go get some events...

        events = Event.objects.filter(meeting_times__in=section_ids).distinct()

        events_by_id = {}
        for e in events:
            events_by_id[e.id] = e
            
        # Now, to combine all of the above:

        for s in sections:
            s._events = [events_by_id[int(x)] for x in s.event_ids.split(',')] if s.event_ids != '' else []
            s._events.sort(cmp=lambda e1, e2: cmp(e1.start, e2.start))

        return sections
    
    @cache_function
    def get_meeting_times(self):
        return self.meeting_times.all()
    get_meeting_times.depend_on_m2m(lambda: ClassSection, 'meeting_times', lambda sec, event: {'self': sec})
    
    #   Some properties for traits that are actually traits of the ClassSubjects.
    def _get_parent_program(self):
        return self.parent_class.parent_program
    parent_program = property(_get_parent_program)
        
    def _get_teachers(self):
        return self.parent_class.teachers()
    teachers = property(_get_teachers)
    
    def _get_category(self):
        return self.parent_class.category
    category = property(_get_category)

    def _get_room_capacity(self, rooms = None):
        if rooms == None:
            rooms = self.initial_rooms()

        rc = 0
        for r in rooms:
            rc += r.num_students

        return rc

    @cache_function
    def _get_capacity(self, ignore_changes=False):
        ans = None
        rooms = self.initial_rooms()
        if self.max_class_capacity is not None:
            ans = self.max_class_capacity
        else:
            if len(rooms) == 0:
                if not ans:
                    ans = self.parent_class.class_size_max
            else:
                ans = min(self.parent_class.class_size_max, self._get_room_capacity(rooms))

        #hacky fix for classes with no max size
        if ans == None or ans == 0:
            # New class size capacity condition set for Splash 2010.  In code
            # because it seems like a fairly reasonable metric.
            if self.parent_class.allowable_class_size_ranges.all() and len(rooms) != 0:
                ans = min(max(self.parent_class.allowable_class_size_ranges.order_by('-range_max').values_list('range_max', flat=True)[0], self.parent_class.class_size_optimal), self._get_room_capacity(rooms))
            elif self.parent_class.class_size_optimal and len(rooms) != 0:
                ans = min(self.parent_class.class_size_optimal, self._get_room_capacity(rooms))
            elif self.parent_class.class_size_optimal:
                ans = self.parent_class.class_size_optimal
            elif len(rooms) != 0:
                ans = self._get_room_capacity(rooms)
            else: 
                ans = 0
            
        #   Apply dynamic capacity rule
        if not ignore_changes:
            options = self.parent_program.getModuleExtension('StudentClassRegModuleInfo')
            return int(ans * options.class_cap_multiplier + options.class_cap_offset)
        else:
            return int(ans)

    _get_capacity.depend_on_m2m(lambda:ClassSection, 'meeting_times', lambda sec, event: {'self': sec})
    _get_capacity.depend_on_row(lambda:ClassSection, lambda r: {'self': r})
    _get_capacity.depend_on_model(lambda:ClassSubject)
    _get_capacity.depend_on_model(lambda: Resource)
    _get_capacity.depend_on_row(lambda:ClassSection, 'self')
    _get_capacity.depend_on_row(lambda:ResourceRequest, lambda r: {'self': r.target})
    _get_capacity.depend_on_row(lambda:ResourceAssignment, lambda r: {'self': r.target})
    def __get_studentclassregmoduleinfo():
        from esp.program.modules.module_ext import StudentClassRegModuleInfo
        return StudentClassRegModuleInfo
    _get_capacity.depend_on_model(__get_studentclassregmoduleinfo)

       
    capacity = property(_get_capacity)

    def title(self):
        return self.parent_class.title()
    
    def __init__(self, *args, **kwargs):
        super(ClassSection, self).__init__(*args, **kwargs)
        self.cache = SectionCacheHelper(self)

    def __unicode__(self):
        return '%s: %s' % (self.emailcode(), self.title())

    cache = ClassCacheHelper

    def index(self):
        """ Get index of this section among those belonging to the parent class. """
        pc = self.parent_class
        pc_sec_ids = map(lambda x: x.id, pc.get_sections())
        return list(pc_sec_ids).index(self.id) + 1

    def delete(self, adminoverride=False):
        if self.num_students() > 0 and not adminoverride:
            return False
        
        self.getResourceRequests().delete()
        self.getResourceAssignments().delete()
        self.meeting_times.clear()
        self.checklist_progress.clear()
        if self.anchor:
            self.anchor.delete(True)
        
        super(ClassSection, self).delete()

    def getResourceAssignments(self):
        from esp.resources.models import ResourceAssignment
        return ResourceAssignment.objects.filter(target=self)

    def getResources(self):
        assignment_list = self.getResourceAssignments()
        return [a.resource for a in assignment_list]
    
    def getResourceRequests(self):
        from esp.resources.models import ResourceRequest
        return ResourceRequest.objects.filter(target=self)
    
    def clearResourceRequests(self):
        for rr in self.getResourceRequests():
            rr.delete()
    
    def classroomassignments(self):
        from esp.resources.models import ResourceType
        cls_restype = ResourceType.get_or_create('Classroom')
        return self.getResourceAssignments().filter(target=self, resource__res_type=cls_restype)
    
    def resourceassignments(self):
        """   Get all assignments pertaining to floating resources like projectors. """
        from esp.resources.models import ResourceType
        cls_restype = ResourceType.get_or_create('Classroom')
        ta_restype = ResourceType.get_or_create('Teacher Availability')
        return self.getResourceAssignments().filter(target=self).exclude(resource__res_type=cls_restype).exclude(resource__res_type=ta_restype)
    
    def classrooms(self):
        """ Returns the list of classroom resources assigned to this class."""
        from esp.resources.models import Resource

        ra_list = self.classroomassignments().values_list('resource', flat=True)
        return Resource.objects.filter(id__in=ra_list)

    def initial_rooms(self):
        from esp.resources.models import Resource
        if len(self.get_meeting_times()) > 0:
            return self.classrooms().filter(event=self.meeting_times.order_by('start')[0]).order_by('id')
        else:
            return Resource.objects.none()

    def prettyrooms(self):
        """ Return the pretty name of the rooms. """
        if self.meeting_times.count() > 0:
            return [x.name for x in self.initial_rooms()]
        else:
            return []
   
    def emailcode(self):
        return self.parent_class.emailcode() + 's' + str(self.index())
   
    def starts_soon(self):
        #   Return true if the class's start time is less than 50 minutes after the current time
        #   and less than 10 minutes before the current time.
        first_block = self.start_time()
        if first_block is None:
            return False
        else:
            st = first_block.start
            

        if st is None:
            return False
        else:
            td = time.time() - time.mktime(st.timetuple())
            if td < 600 and td > -3000:
                return True
            else:
                return False
            
    def already_passed(self):
        from datetime import timedelta
        start_time = self.start_time()
        if start_time is None:
            return True
        time_passed = datetime.datetime.now() - start_time.start
        if self.parent_class.allow_lateness:
            if time_passed > timedelta(0, 1200):
                return True
        else:
            if time_passed > timedelta(0):
                return True
        return False
   
    def start_time(self):
        if self.meeting_times.count() > 0:
            return self.meeting_times.order_by('start')[0]
        else:
            return None
   
    def pretty_start_time(self):
        s = self.start_time()
        if s:
            return s.short_time()
        else:
            return 'N/A'

    #   Scheduling helper functions

    @cache_function
    def sufficient_length(self, event_list=None):
        """   This function tells if the class' assigned times are sufficient to cover the duration.
        If the duration is not set, 1 hour is assumed. """
        
        duration = self.duration or 1.0

        if event_list is None:
            event_list = list(self.meeting_times.all().order_by('start'))
        #   If you're 15 minutes short that's OK.
        time_tolerance = 15 * 60
        if Event.total_length(event_list).seconds + time_tolerance < duration * 3600:
            return False
        else:
            return True
    sufficient_length.depend_on_m2m(lambda:ClassSection, 'meeting_times', lambda sec, event: {'self': sec})
    
    
    def extend_timeblock(self, event, merged=True):
        """ Return the Event list or (merged Event) for this class's duration if the class starts in the
        provided timeslot and continues contiguously until its duration has ended. """
        
        event_list = [event]
        all_events = list(self.parent_program.getTimeSlots())
        event_index = all_events.index(event)

        while not self.sufficient_length(event_list):
            event_index += 1
            event_list.append(all_events[event_index])
            
        if merged:
            return Event.collapse(event_list, tol=datetime.timedelta(minutes=10))
        else:
            return event_list
    
    def scheduling_status(self):
        cache_key = "CLASSSECTION__SCHEDULING_STATUS__%s" % self.id
        retVal = cache.get(cache_key)
        if retVal:
            return retVal
        
        #   Return a little string that tells you what's up with the resource assignments.
        if not self.sufficient_length():
            retVal = 'Needs time'
        elif self.classrooms().count() < 1:
            retVal = 'Needs room'
        elif self.unsatisfied_requests().count() > 0:
            retVal = 'Needs resources'
        else:
            retVal = 'Happy'

        cache.set(cache_key, retVal, timeout=60)
        return retVal
            
    def clear_resource_cache(self):
        from django.core.cache import cache
        from esp.resources.models import increment_global_resource_rev
        cache_key1 = 'class__viable_times:%d' % self.id
        cache_key2 = 'class__viable_rooms:%d' % self.id
        cache_key3 = "CLASSSECTION__SUFFICIENT_LENGTH__%s" % self.id
        cache.delete(cache_key1)
        cache.delete(cache_key2)
        cache.delete(cache_key3)
        increment_global_resource_rev()
    
    def unsatisfied_requests(self):
        from esp.resources.models import global_resource_rev
        cache_key = "CLASSSECTION__UNSATISFIED_REQUESTS__%s__%s" % (self.id, global_resource_rev())

        retVal = cache.get(cache_key)
        if retVal:
            return retVal
        
        if self.classrooms().count() > 0:
            primary_room = self.classrooms()[0]
            result = primary_room.satisfies_requests(self)[1]
            cache.set(cache_key, result, timeout=86400)
            return result
        else:
            result = self.getResourceRequests()
            cache.set(cache_key, result, timeout=86400)
            return result
    
    def assign_meeting_times(self, event_list):
        self.meeting_times.clear()
        for event in set(event_list):
            self.meeting_times.add(event)

    def clear_meeting_times(self):
        self.meeting_times.clear()
    
    def assign_start_time(self, first_event):
        """ Get enough events following the first one until you have the class duration covered.
        Then add them. """

        #   This means we have to clear the classrooms.
        #   But we will try to re-assign the same room at the new times if it is available.
        current_rooms = self.initial_rooms()
        
        self.clearRooms()
        self.clearFloatingResources()
        
        event_list = self.extend_timeblock(first_event, merged=False)
        self.assign_meeting_times(event_list)
        
        #   Check to see if the desired rooms are available at the new times
        availability = True
        for e in event_list:
            for room in current_rooms:
                if not room.is_available(e):
                    availability = False
                    
        #   If the desired rooms are available, assign them.  (If not, no big deal.)
        if availability:
            for room in current_rooms:
                self.assign_room(room)

        cache_key = "CLASSSECTION__SUFFICIENT_LENGTH__%s" % self.id
        cache.delete(cache_key)
    
    def assign_room(self, base_room, compromise=True, clear_others=False, allow_partial=False):
        """ Assign the classroom given, at the times needed by this class. """
        rooms_to_assign = base_room.identical_resources().filter(event__in=list(self.meeting_times.all()))
        
        status = True
        errors = []
        
        if clear_others:
            self.clearRooms()
        
        if compromise is False:
            #   Check that the room satisfies all needs of the class.
            result = base_room.satisfies_requests(self)
            if result[0] is False:
                status = False
                errors.append( u'Room %s lacks some resources that %s needs (or is too small), and you opted not to compromise.' % (base_room.name, self.emailcode()) )
        
        if rooms_to_assign.count() != self.meeting_times.count():
            status = False
            errors.append( u'Room %s does not exist at the times requested by %s.' % (base_room.name, self.emailcode()) )
        
        for i, r in enumerate(rooms_to_assign):
            r.clear_schedule_cache(self.parent_program)
            result = self.assignClassRoom(r)
            if not result:
                status = False
                occupiers_str = ''
                occupiers_set = r.assignments()
                if occupiers_set.count() > 0: # We really shouldn't have to test for this, but I guess it's safer not to assume... -ageng 2008-11-02
                    occupiers_str = u' by %s during %s' % ((occupiers_set[0].target or occupiers_set[0].target_subj).emailcode(), r.event.pretty_time())
                errors.append( u'Room %s is occupied%s.' % ( base_room.name, occupiers_str ) )
                # If we don't allow partial fulfillment, undo and quit.
                if not allow_partial:
                    for r2 in rooms_to_assign[:i]:
                        r2.clear_assignments()
                    break
            
        return (status, errors)
    
    @cache_function
    def viable_times(self, ignore_classes=False):
        """ Return a list of Events for which all of the teachers are available. """
        
        def intersect_lists(list_of_lists):
            if len(list_of_lists) == 0:
                return []

            base_list = list_of_lists[0]
            for other_list in list_of_lists[1:]:
                i = 0
                for elt in base_list:
                    if elt not in other_list:
                        base_list.remove(elt)
            return base_list

        teachers = self.parent_class.teachers()
        try:
            num_teachers = teachers.count()
        except:
            num_teachers = len(teachers)

        timeslot_list = []
        for t in teachers:
            timeslot_list.append(list(t.getAvailableTimes(self.parent_program, ignore_classes)))
            
        available_times = intersect_lists(timeslot_list)
        
        #   If the class is already scheduled, put its time in.
        if self.meeting_times.count() > 0:
            for k in self.meeting_times.all():
                if k not in available_times:
                    available_times.append(k)
        
        timeslots = Event.group_contiguous(available_times)

        viable_list = []

        for timegroup in timeslots:
            for i in range(0, len(timegroup)):
                #   Check whether there is enough time remaining in the block.
                if self.sufficient_length(timegroup[i:len(timegroup)]):
                    viable_list.append(timegroup[i])

        return viable_list
    #   Dependencies: 
    #   - teacher availability
    #   - teachers of the class
    #   - the target section and its meeting times
    viable_times.depend_on_model(lambda:UserAvailability)   #   To do: Make this more specific (so the cache doesn't get flushed so often)
    viable_times.depend_on_m2m(lambda:ClassSection, 'meeting_times', lambda sec, event: {'self': sec})
    viable_times.depend_on_row(lambda:ClassSection, lambda sec: {'self': sec})
    @staticmethod
    def key_set_from_userbit(bit):
        sections = ClassSection.objects.filter(QTree(anchor__below=bit.qsc))
        return [{'self': sec} for sec in sections]
    viable_times.depend_on_row(lambda:UserBit, lambda bit: ClassSection.key_set_from_userbit(bit), lambda bit: bit.verb == GetNode('V/Flags/Registration/Teacher'))

    def viable_rooms(self):
        """ Returns a list of Resources (classroom type) that satisfy all of this class's resource requests. 
        Resources matching the first time block of the class will be returned. """
        from django.core.cache import cache
        from esp.resources.models import ResourceType, Resource
        import operator
        
        def room_satisfies_times(room, times):
            room_times = room.matching_times()
            satisfaction = True
            for t in times:
                if t not in room_times:
                    satisfaction = False
            return satisfaction
        
        #   This will need to be cached.
        cache_key = 'class__viable_rooms:%d' % self.id
        result = cache.get(cache_key)
        if result is not None:
            return result
        
        #   This function is only meaningful if the times have already been set.  So, back out if they haven't.
        if not self.sufficient_length():
            return []
        
        #   Start with all rooms the program has.  
        #   Filter the ones that are available at all times needed by the class.
        filter_qs = []
        ordered_times = self.meeting_times.order_by('start')
        first_time = ordered_times[0]
        possible_rooms = self.parent_program.getAvailableClassrooms(first_time)
        
        viable_list = filter(lambda x: room_satisfies_times(x, ordered_times), possible_rooms)
            
        cache.set(cache_key, viable_list)
        return viable_list
    
    def clearRooms(self):
        for room in [ra.resource for ra in self.classroomassignments()]:
            room.clear_schedule_cache(self.parent_program)
        self.classroomassignments().delete()
            
    def clearFloatingResources(self):
        self.resourceassignments().delete()

    def assignClassRoom(self, classroom):
        #   Assign an individual resource to this class.
        from esp.resources.models import ResourceAssignment
        
        if classroom.is_taken():
            return False
        else:
            new_assignment = ResourceAssignment()
            new_assignment.resource = classroom
            new_assignment.target = self
            new_assignment.save()
            return True

    @cache_function
    def timeslot_ids(self):
        return self.meeting_times.all().values_list('id', flat=True)
    timeslot_ids.depend_on_m2m(lambda: ClassSection, 'meeting_times', lambda instance, object: {'self': instance})

    def cannotAdd(self, user, checkFull=True, use_cache=True):
        """ Go through and give an error message if this user cannot add this section to their schedule. """
        # Test any scheduling constraints
        relevantConstraints = self.parent_program.getScheduleConstraints()
        #   relevantConstraints = ScheduleConstraint.objects.none()

        if relevantConstraints:
            # Set up a ScheduleMap; fake-insert this class into it
            sm = ScheduleMap(user, self.parent_program)
            sm.add_section(self)

            for exp in relevantConstraints:
                if not exp.evaluate(sm):
                    return "You're violating a scheduling constraint.  Adding <i>%s</i> to your schedule requires that you: %s." % (self.title(), exp.requirement.label)
        
        scrmi = self.parent_program.getModuleExtension('StudentClassRegModuleInfo')
        if not scrmi.use_priority:
            verbs = ['Enrolled']
            section_list = user.getEnrolledSectionsFromProgram(self.parent_program)
        else:
            verbs = [scrmi.signup_verb.name]
            # Disallow joining a no-app class that conflicts with an app class
            # For HSSP Harvard Spring 2010
            #if self.parent_class.studentappquestion_set.count() == 0:
            #    verbs += ['/Applied']
            section_list = user.getSections(self.parent_program, verbs=verbs)
        
        # check to see if there's a conflict:
        my_timeslots = self.timeslot_ids()
        for sec in section_list:
            if sec.parent_class == self.parent_class:
                return 'You are already signed up for a section of this class!'
            if hasattr(sec, '_timeslot_ids'):
                timeslot_ids = sec._timeslot_ids
            else:
                timeslot_ids = sec.timeslot_ids()
            for tid in timeslot_ids:
                if tid in my_timeslots:
                    return 'This section conflicts with your schedule--check out the other sections!'
                    
        # check to see if registration has been closed for this section
        if not self.isRegOpen():
            return 'Registration for this section is not currently open.'

        # check to make sure they haven't already registered for too many classes in this section
        if scrmi.use_priority:
            priority = user.getRegistrationPriority(self.parent_class.parent_program, self.meeting_times.all())
            if priority > scrmi.priority_limit:
                return 'You are only allowed to select up to %s top classes' % (scrmi.priority_limit)

        # this user *can* add this class!
        return False

    def conflicts(self, teacher, meeting_times=None):
        """Return a scheduling conflict if one exists, or None."""
        from esp.users.models import ESPUser
        user = ESPUser(teacher)
        if user.getTaughtClasses().count() == 0:
            return None
        if meeting_times is None:
            meeting_times = self.meeting_times.all()
        for sec in user.getTaughtSections(self.parent_program).exclude(id=self.id):
            for time in sec.meeting_times.all():
                if meeting_times.filter(id = time.id).count() > 0:
                    return (sec, time)

        return None

    def cannotSchedule(self, meeting_times):
        """
        Returns False if the given times work; otherwise, an error message.

        Assumes meeting_times is a sorted QuerySet of correct length.

        """
        if meeting_times[0] not in self.viable_times(ignore_classes=True):
            # This set of error messages deserves a better home
            for t in self.teachers:
                available = t.getAvailableTimes(self.parent_program, ignore_classes=False)
                for e in meeting_times:
                    if e not in available:
                        return "The teacher %s has not indicated availability during %s." % (t.name(), e.pretty_time())
                conflicts = self.conflicts(t, meeting_times)
                if conflicts:
                    return "The teacher %s is teaching %s during %s." % (t.name(), conflicts[0].emailcode(), conflicts[1].pretty_time())
            # Fallback in case we couldn't come up with details
            return "Some of the teachers (could not determine which) are unavailable at this time."
        return False

    def students_dict_old(self):
        verb_base = DataTree.get_by_uri('V/Flags/Registration')
        uri_start = len(verb_base.get_uri())
        result = defaultdict(list)
        userbits = UserBit.objects.filter(QTree(verb__below = verb_base), qsc=self.anchor).filter(enddate__gte=datetime.datetime.now()).distinct()
        for u in userbits:
            bit_str = u.verb.get_uri()[uri_start:]
            result[bit_str].append(ESPUser(u.user))
        return PropertyDict(result)

    #   If the values returned by this function are ever needed in QuerySet form,
    #   something will need to be changed.
    @cache_function
    def students_dict(self):
        from esp.program.models import RegistrationType
        now = datetime.datetime.now()
        
        rmap = RegistrationType.get_map()
        result = {}
        for key in rmap:
            result[key] = list(self.registrations.filter(studentregistration__relationship=rmap[key], studentregistration__start_date__lte=now, studentregistration__end_date__gte=now).distinct())
            if len(result[key]) == 0:
                del result[key]
        return result
    students_dict.depend_on_row(lambda: StudentRegistration, lambda reg: {'self': reg.section})
    
    def students_prereg(self):
        now = datetime.datetime.now()
        return self.registrations.filter(studentregistration__start_date__lte=now, studentregistration__end_date__gte=now).distinct()

    def students(self, verbs=['Enrolled']):
        now = datetime.datetime.now()
        result = ESPUser.objects.none()
        for verb_str in verbs:
            result = result | self.registrations.filter(studentregistration__relationship__name=verb_str, studentregistration__start_date__lte=now, studentregistration__end_date__gte=now)
        return result.distinct()
    
    @cache_function
    def num_students_prereg(self):
        return self.students_prereg().count()
    num_students_prereg.depend_on_row(lambda: StudentRegistration, lambda reg: {'self': reg.section})

    @cache_function
    def num_students(self, verbs=['Enrolled']):
        if verbs == ['Enrolled']:
            if not hasattr(self, '_count_students'):
                self._count_students = self.students(verbs).count()
            return self._count_students
        return self.students(verbs).count()
    num_students.depend_on_row(lambda: StudentRegistration, lambda reg: {'self': reg.section})

    @cache_function
    def count_enrolled_students(self):
        return self.num_students(use_cache=False)
    count_enrolled_students.depend_on_row(lambda: StudentRegistration, lambda reg: {'self': reg.section})

    enrolled_students = DerivedField(models.IntegerField, count_enrolled_students)(null=False, default=0)

    def cancel(self, email_students=True, explanation=None):
        from esp.settings import INSTITUTION_NAME, ORGANIZATION_SHORT_NAME, DEFAULT_EMAIL_ADDRESSES
        from django.contrib.sites.models import Site
        from esp.dbmail.models import send_mail

        context = {'sec': self, 'prog': self.parent_program, 'explanation': explanation}
        context['full_group_name'] = Tag.getTag('full_group_name') or '%s %s' % (INSTITUTION_NAME, ORGANIZATION_SHORT_NAME)
        context['site_url'] = Site.objects.get_current().domain
        context['email_students'] = email_students
        context['num_students'] = self.num_students()
        email_title = 'Class Cancellation at %s - Section %s' % (self.parent_program.niceName(), self.emailcode())
        if email_students:
            email_content = render_to_string('email/class_cancellation.txt', context)
            template = Template(email_content)
            #   Send e-mail to each student
            for student in self.students():
                to_email = ['%s <%s>' % (student.name(), student.email)]
                from_email = '%s at %s <%s>' % (self.parent_program.anchor.parent.friendly_name, INSTITUTION_NAME, self.parent_program.director_email)
                msgtext = template.render(Context({'user': student}))
                send_mail(email_title, msgtext, from_email, to_email)

        #   Send e-mail to administrators as well
        email_content = render_to_string('email/class_cancellation_admin.txt', context)
        to_email = ['Directors <%s>' % (self.parent_program.director_email)]
        from_email = '%s Web Site <%s>' % (self.parent_program.anchor.parent.friendly_name, self.parent_program.director_email)
        send_mail(email_title, email_content, from_email, to_email)

        self.clearStudents()
    
        self.status = -20
        self.save()

    def clearStudents(self):
        from esp.program.models import StudentRegistration
        now = datetime.datetime.now()
        qs = StudentRegistration.objects.filter(section=self, end_date__gte=now)
        qs.update(end_date=now)
        #   Compensate for the lack of a signal on update().
        for reg in qs:
            signals.post_save.send(sender=StudentRegistration, instance=reg)

    @staticmethod
    def idcmp(one, other):
        return cmp(one.id, other.id)

    def __cmp__(self, other):
        selfevent = self.firstBlockEvent()
        otherevent = other.firstBlockEvent()

        if selfevent is not None and otherevent is None:
            return 1
        if selfevent is None and otherevent is not None:
            return -1

        if selfevent is not None and otherevent is not None:
            cmpresult = selfevent.__cmp__(otherevent)
            if cmpresult != 0:
                return cmpresult

        return cmp(self.title(), other.title())


    def firstBlockEvent(self):
        eventList = self.meeting_times.all().order_by('start')
        if eventList.count() == 0:
            return None
        else:
            return eventList[0]

    def room_capacity(self):
        ir = self.initial_rooms()
        if ir.count() == 0:
            return 0
        else:
            return reduce(lambda x,y: x+y, [r.num_students for r in ir]) 
            
    def isFull(self, ignore_changes=False):
        if (self.num_students() == self._get_capacity(ignore_changes) == 0):
            return False
        else:
            return (self.num_students() >= self._get_capacity(ignore_changes))
            
    def time_blocks(self):
        return self.friendly_times(raw=True)

    def friendly_times(self, use_cache=False, raw=False, include_date=False): # if include_date is True, display the date as well (e.g., display "Sun, July 10" instead of just "Sun"
        """ Return a friendlier, prettier format for the times.

        If the events of this class are next to each other (within 10-minute overlap,
        the function will automatically collapse them. Thus, instead of
           ['11:00am--12:00n','12:00n--1:00pm'],
           
        you would get
           ['11:00am--1:00pm']
        for instance.
        """
        from esp.cal.models import Event
        from esp.resources.models import ResourceAssignment, ResourceType, Resource

        retVal = self.cache['friendly_times_%s' % raw]

        if retVal is not None and use_cache:
            return retVal
            
        txtTimes = []
        eventList = []
        
        # For now, use meeting times lookup instead of resource assignments.
        """
        classroom_type = ResourceType.get_or_create('Classroom')
        resources = Resource.objects.filter(resourceassignment__target=self).filter(res_type=classroom_type)
        events = [r.event for r in resources] 
        """
        if hasattr(self, "_events"):
            events = list(self._events)
        else:
            events = list(self.meeting_times.all())

        if raw:
            txtTimes = Event.collapse(events, tol=datetime.timedelta(minutes=15))
        else:
            txtTimes = [ event.pretty_time(include_date=include_date) for event
                     in Event.collapse(events, tol=datetime.timedelta(minutes=15)) ]

        self.cache['friendly_times_%s' % raw] = txtTimes

        return txtTimes
    
    def friendly_times_with_date(self, use_cache=False, raw=False): 
        return self.friendly_times(use_cache=use_cache, raw=raw, include_date=True)
    
    def isAccepted(self): return self.status == 10
    def isReviewed(self): return self.status != 0
    def isRejected(self): return self.status == -10
    def isCancelled(self): return self.status == -20
    isCanceled = isCancelled   
    def isRegOpen(self): return self.registration_status == 0
    def isRegClosed(self): return self.registration_status == 10

    def update_cache(self):
        self.cache.update()
    update_cache_students = update_cache

    def save(self, *args, **kwargs):
        super(ClassSection, self).save(*args, **kwargs)
        self.update_cache()

    def getRegistrations(self, user):
        from esp.program.models import StudentRegistration
        now = datetime.datetime.now()
        return StudentRegistration.objects.filter(section=self, user=user, start_date__lte=now, end_date__gte=now).order_by('start_date')
    
    def getRegVerbs(self, user, allowed_verbs=False):
        """ Get the list of verbs that a student has within this class's anchor. """
        if not allowed_verbs:
            return self.getRegistrations(user).values_list('relationship__name', flat=True).distinct()
        else:
            return self.getRegistrations(user).filter(relationship__name__in=allowed_verbs).values_list('relationship__name', flat=True).distinct()

    def unpreregister_student(self, user, prereg_verb = None):
        #   New behavior: prereg_verb should be a string matching the name of
        #   RegistrationType to match (if you want to use it)
        
        from esp.program.models.app_ import StudentAppQuestion
        from esp.program.models import StudentRegistration
        
        now = datetime.datetime.now()
        
        #   Stop all active or pending registrations
        if prereg_verb:
            qs = StudentRegistration.objects.filter(relationship__name=prereg_verb, section=self, user=user, end_date__gte=now)
            qs.update(end_date=now)
            #   print 'Expired %s' % qs
        else:
            qs = StudentRegistration.objects.filter(section=self, user=user, end_date__gte=now)
            qs.update(end_date=now)
            #   print 'Expired %s' % qs
            
        #   Explicitly fire the signals for saving a StudentRegistration in order to update caches
        #   since it doesn't get sent by update() above
        if qs.exists():
            signals.post_save.send(sender=StudentRegistration, instance=qs[0])
            
        #   If the student had blank application question responses for this class, remove them.
        app = ESPUser(user).getApplication(self.parent_program, create=False)
        if app:
            blank_responses = app.responses.filter(question__subject=self.parent_class, response='')
            unneeded_questions = StudentAppQuestion.objects.filter(studentappresponse__in=blank_responses)
            for q in unneeded_questions:
                app.questions.remove(q)
            blank_responses.delete()
        
        # Remove the student from any existing class mailing lists
        list_names = ["%s-%s" % (self.emailcode(), "students"), "%s-%s" % (self.parent_class.emailcode(), "students")]
        for list_name in list_names:
            remove_list_member(list_name, user.email)

    
    from esp.program.models import StudentRegistration, RegistrationType
    def preregister_student(self, user, overridefull=False, priority=1, prereg_verb = None, fast_force_create=False):
        StudentRegistration, RegistrationType = self.StudentRegistration, self.RegistrationType

        if prereg_verb == None:
            scrmi = self.parent_program.getModuleExtension('StudentClassRegModuleInfo')
            if scrmi and scrmi.use_priority:
                prereg_verb = 'Priority/%d' % priority
            else:
                prereg_verb = 'Enrolled'

        if overridefull or fast_force_create or not self.isFull():
            #    Then, create the registration for this class.
            now = datetime.datetime.now()
            
            rt = RegistrationType.get_cached(name=prereg_verb, category='student')
            qs = self.registrations.filter(id=user.id, studentregistration__start_date__lte=now, studentregistration__end_date__gte=now, studentregistration__relationship=rt)
            if fast_force_create or not qs.exists():
                sr = StudentRegistration(user=user, section=self, relationship=rt)
                sr.save()
                #   print 'Created %s' % sr
                if fast_force_create:
                    ## That's the bare minimum to reg someone; we're done!
                    return True
            
                # If the registration was placed through OnSite Reg, annotate it as an OnSite registration
                onsite_verb = 'OnSite/ChangedClasses'
                request = get_current_request()
                if request and request.user and isinstance(request.user, ESPUser) and request.user.is_morphed(request):
                    rt, created = RegistrationType.objects.get_or_create(name=onsite_verb, category='student')
                    sr = StudentRegistration(user=user, section=self, relationship=rt)
                    sr.save()
                    #   print 'Created %s' % sr
            else:
                #   print 'Already in class: %s' % qs
                pass

            if self.parent_program.isUsingStudentApps():
                #   Clear completion bit on the student's application if the class has app questions.
                app = ESPUser(user).getApplication(self.parent_program, create=False)
                if app:
                    app.set_questions()
                    if app.questions.count() > 0:
                        app.done = False
                        app.save()

            #   Add the student to the class mailing lists, if they exist
            list_names = ["%s-%s" % (self.emailcode(), "students"), "%s-%s" % (self.parent_class.emailcode(), "students")]
            for list_name in list_names:
                add_list_member(list_name, user.email)
            add_list_member("%s_%s-students" % (self.parent_program.anchor.parent.name, self.parent_program.anchor.name), user.email)

            return True
        else:
            #    Pre-registration failed because the class is full.
            return False

    def pageExists(self):
        from esp.qsd.models import QuasiStaticData
        return len(self.anchor.quasistaticdata_set.filter(name='learn:index').values('id')[:1]) > 0

    def prettyDuration(self):
        if self.duration is None:
            return 'N/A'

        return '%s:%02d' % \
               (int(self.duration),
            int((self.duration - int(self.duration)) * 60))

    class Meta:
        db_table = 'program_classsection'
        app_label = 'program'
        ordering = ['anchor__name']

class ClassSubject(models.Model, CustomFormsLinkModel):
    """ An ESP course.  The course includes one or more ClassSections which may be linked by ClassImplications. """
    
    #customforms info
    form_link_name='Course'	

    from esp.program.models import Program
    
    anchor = AjaxForeignKey(DataTree)
    parent_program = models.ForeignKey(Program)
    category = models.ForeignKey('ClassCategories',related_name = 'cls')
    class_info = models.TextField(blank=True)
    allow_lateness = models.BooleanField(default=False)
    message_for_directors = models.TextField(blank=True)
    class_size_optimal = models.IntegerField(blank=True, null=True)
    optimal_class_size_range = models.ForeignKey(ClassSizeRange, blank=True, null=True)
    allowable_class_size_ranges = models.ManyToManyField(ClassSizeRange, related_name='classsubject_allowedsizes', blank=True, null=True)
    grade_min = models.IntegerField()
    grade_max = models.IntegerField()
    class_size_min = models.IntegerField(blank=True, null=True)
    hardness_rating = models.TextField(blank=True, null=True)
    class_size_max = models.IntegerField(blank=True, null=True)
    schedule = models.TextField(blank=True)
    prereqs  = models.TextField(blank=True, null=True)
    requested_special_resources = models.TextField(blank=True, null=True)
    directors_notes = models.TextField(blank=True, null=True)
    checklist_progress = models.ManyToManyField(ProgramCheckItem, blank=True)
    requested_room = models.TextField(blank=True, null=True)
    session_count = models.IntegerField(default=1)

    purchase_requests = models.TextField(blank=True, null=True)
    custom_form_data = JSONField(blank=True, null=True)
    
    objects = ClassManager()
    checklist_progress_all_cached = checklist_progress_base('ClassSubject')

    #   Backwards compatibility with Class database format.
    #   Please don't use. :)
    status = models.IntegerField(default=0)   
    duration = models.DecimalField(blank=True, null=True, max_digits=5, decimal_places=2)
    meeting_times = models.ManyToManyField(Event, blank=True)

    @cache_function
    def get_allowable_class_size_ranges(self):
        return self.allowable_class_size_ranges.all()
    get_allowable_class_size_ranges.depend_on_m2m(lambda:ClassSubject, 'allowable_class_size_ranges', lambda subj, csr: {'self':subj })

    def get_sections(self):
        if not hasattr(self, "_sections") or self._sections is None:
            self._sections = self.sections.all()

        return self._sections
        
    @classmethod
    def ajax_autocomplete(cls, data):
        values = cls.objects.filter(anchor__friendly_name__istartswith=data).values(
                    'id', 'anchor__friendly_name').order_by('anchor__friendly_name')
        for v in values:
            v['ajax_str'] = v['anchor__friendly_name']
        return values
    
    def ajax_str(self):
        return self.title()
    
    def prettyDuration(self):
        if len(self.get_sections()) <= 0:
            return "N/A"
        else:
            return self.get_sections()[0].prettyDuration()

    def prettyrooms(self):
        if len(self.get_sections()) <= 0:
            return "N/A"
        else:
            return self.get_sections()[0].prettyrooms()

    def ascii_info(self):
        return self.class_info.encode('ascii', 'ignore')
        
    def _get_meeting_times(self):
        timeslot_id_list = []
        for s in self.get_sections():
            timeslot_id_list += s.meeting_times.all().values_list('id', flat=True)
        return Event.objects.filter(id__in=timeslot_id_list).order_by('start')
    all_meeting_times = property(_get_meeting_times)

    def _get_capacity(self):
        c = 0
        for s in self.get_sections():
            c += s.capacity
        return c
    capacity = property(_get_capacity)

    def __init__(self, *args, **kwargs):
        super(ClassSubject, self).__init__(*args, **kwargs)
        self.cache = ClassSubject.objects.cache(self)

    def get_section(self, timeslot=None):
        """ Cache sections for a class.  Always use this function to get a class's sections. """
        # If we happen to know our own sections from a subquery:
        did_search = True

        if hasattr(self, "_sections"):
            for s in self._sections:
                if not hasattr(s, "_events"):
                    did_search = False
                    break
                if timeslot in s._events or timeslot == None:
                    return s

            if did_search: # If we did successfully search all sections, but found none in this timeslot
                return None
            #If we didn't successfully search all sections, go and do it the old-fashioned way:
            
        from django.core.cache import cache

        if timeslot:
            key = 'Sections_SubjectID%d_TimeslotID%d' % (self.id, timeslot.id)
        else:
            key = 'Sections_SubjectID%d_Default' % self.id

        # Encode None as a string... silly, I know.   -Michael P
        val = cache.get(key) 
        if val:
            if val is not None:
                if val == 'None':
                    return None
                else:
                    return val
        
        if timeslot:
            qs = self.sections.filter(meeting_times=timeslot)
            if qs.count() > 0:
                result = qs[0]
            else:
                result = None
        else:
            result = self.default_section()
            
        if result is not None:
            cache.set(key, result)
        else:
            cache.set(key, 'None')

        return result

    def default_section(self, create=True):
        """ Return the first section that was created for this class. """
        sec_qs = self.sections.order_by('id')
        if sec_qs.count() == 0:
            if create:
                return self.add_default_section()
            else:
                return None
        else:
            return sec_qs[0]

    def add_section(self, duration=None, status=None):
        """ Add a ClassSection belonging to this class. Can be run multiple times. """
        
        section_index = self.sections.count() + 1
        
        if duration is None:
            duration = self.duration
        if status is None:
            status = self.status
        
        new_section = ClassSection()
        new_section.parent_class = self
        new_section.duration = '%.4f' % duration
        new_section.anchor = DataTree.get_by_uri(self.anchor.get_uri() + '/Section' + str(section_index), create=True)
        new_section.status = status
        new_section.save()
        self.sections.add(new_section)
        
        self._sections = None
        
        return new_section

    def add_default_section(self, duration=0.0, status=0):
        """ Make sure this class has a section associated with it.  This should be called
        at least once on every class.  Afterwards, additional sections can be created using
        add_section. """
        
        #   Support migration from currently existing classes.
        if self.status != 0:
            status = self.status
        if self.duration is not None and self.duration > 0:
            duration = self.duration
        
        if self.sections.count() == 0:
            return self.add_section(duration, status)
        else:
            return None

    def time_created(self):
        #   Return the datetime for when the class was first created.
        #   Oh wait, this is definitely not meh.
        v = GetNode('V/Flags/Registration/Teacher')
        q = self.anchor
        ubl = UserBit.objects.filter(verb=v, qsc=q).order_by('startdate')
        if ubl.count() > 0:
            return ubl[0].startdate
        else:
            return None
        
    def friendly_times(self):
        collapsed_times = []
        for s in self.get_sections():
            collapsed_times += s.friendly_times()
        return collapsed_times
        
    def students_dict(self):
        result = PropertyDict({})
        for sec in self.get_sections():
            result.merge(sec.students_dict())
        return result
        
    def students(self, verbs=['Enrolled']):
        result = ESPUser.objects.none()
        for sec in self.get_sections():
            result = result | sec.students(verbs=verbs)
        return result
        
    def num_students(self, verbs=['Enrolled']):
        result = 0
        for sec in self.get_sections():
            result += sec.num_students(verbs)
        return result

    def num_students_prereg(self):
        result = 0
        for sec in self.get_sections():
            result += sec.num_students_prereg()
        return result
        
    def max_students(self):
        return self.sections.count()*self.class_size_max

    def fraction_full(self):
        try:
            return self.num_students()/self.max_students()
        except ZeroDivisionError:
            return 1.0

    def emailcode(self):
        """ Return the emailcode for this class.

        The ``emailcode`` is defined as 'first letter of category' + id.
        """
        return self.category.symbol+str(self.id)

    def url(self):
        str_array = self.anchor.tree_encode()
        return '/'.join(str_array[-4:])

    def got_qsd(self):
        """ Returns if this class has any associated QSD. """
        if QuasiStaticData.objects.filter(path = self.anchor)[:1]:
            return True
        else:
            return False

    def got_index_qsd(self):
        """ Returns if this class has an associated index.html QSD. """
        if hasattr(self, "_index_qsd"):
            return (self._index_qsd != '')
        
        if QuasiStaticData.objects.filter(path = self.anchor, name = "learn:index")[:1]:
            return True
        else:
            return False
        
    def __unicode__(self):
        if self.title() is not None:
            return "%s: %s" % (self.id, self.title())
        else:
            return "%s: (none)" % self.id

    def delete(self, adminoverride = False):
        from esp.qsdmedia.models import Media
        
        anchor = self.anchor
        # SQL's cascading delete thing is sketchy --- if the anchor's corrupt,
        # we want webmin manual intervention
        if anchor and not anchor.name.endswith(str(self.id)):
            raise ESPError("Tried to delete class %d with corrupt anchor." % self.id)

        if self.num_students() > 0 and not adminoverride:
            return False
        
        teachers = self.teachers()
        for teacher in self.teachers():
            self.removeTeacher(teacher)
            self.removeAdmin(teacher)

        for sec in self.sections.all():
            sec.delete()
        
        #   Remove indirect dependencies
        Media.objects.filter(QTree(anchor__below=self.anchor)).delete()
        UserBit.objects.filter(QTree(qsc__below=self.anchor)).delete()
        
        self.checklist_progress.clear()
        
        super(ClassSubject, self).delete()
        
        if anchor:
            anchor.delete(True)
                
    def numStudentAppQuestions(self):
        # This field may be prepopulated by .objects.catalog()
        if not hasattr(self, "_studentapps_count"):
            self._studentapps_count = self.studentappquestion_set.count()
            
        return self._studentapps_count
        
        
    def cache_time(self):
        return 99999
    
    @cache_function
    def title(self):
        return self.anchor.friendly_name
        
    def title_selector(node):
        if node.classsubject_set.all().count() == 1:
            return {'self': node.classsubject_set.all()[0]}
        return {}
    title.depend_on_row(lambda: DataTree, title_selector)

    @cache_function
    def teachers(self):
        """ Return a queryset of all teachers of this class. """
        # We might have teachers pulled in by Awesome Query Magic(tm), as in .catalog()
        if hasattr(self, "_teachers"):
            return self._teachers

        v = GetNode('V/Flags/Registration/Teacher')

        # NOTE: This ignores the recursive nature of UserBits, since it's very slow and kind of pointless here.
        # Remove the following line and replace with
        #     retVal = UserBit.objects.bits_get_users(self.anchor, v, user_objs=True)
        # to reenable.
        retVal = ESPUser.objects.all().filter(Q(userbit__qsc=self.anchor, userbit__verb=v), UserBit.not_expired('userbit')).distinct()

        list(retVal)

        return retVal
    @staticmethod
    def key_set_from_userbit(bit):
        subjects = ClassSubject.objects.filter(anchor=bit.qsc)
        return [{'self': cls} for cls in subjects]
    teachers.depend_on_row(lambda:ClassSubject, lambda cls: {'self': cls})
    teachers.depend_on_row(lambda:UserBit, lambda bit: ClassSubject.key_set_from_userbit(bit), lambda bit: bit.verb == GetNode('V/Flags/Registration/Teacher'))

    def pretty_teachers(self, use_cache = True):
        """ Return a prettified string listing of the class's teachers """

        return ", ".join([ "%s %s" % (u.first_name, u.last_name) for u in self.teachers() ])
        
    def isFull(self, ignore_changes=False, timeslot=None):
        """ A class subject is full if all of its sections are full. """
        if timeslot is not None:
            sections = [self.get_section(timeslot)]
        else:
            sections = self.get_sections()
        for s in sections:
            if len(s.get_meeting_times()) > 0 and not s.isFull(ignore_changes=ignore_changes):
                return False
        return True

    @cache_function
    def get_capacity_factor():
        tag_val = Tag.getTag('nearly_full_threshold')
        if tag_val:
            capacity_factor = float(tag_val)
        else:
            capacity_factor = 0.75
        return capacity_factor
    get_capacity_factor.depend_on_row(lambda: Tag, lambda tag: {}, lambda tag: tag.key == 'nearly_full_threshold')
    get_capacity_factor = staticmethod(get_capacity_factor)

    def is_nearly_full(self, capacity_factor = None):
        if capacity_factor == None:
            capacity_factor = get_capacity_factor()
        return len([x for x in self.get_sections() if x.num_students() > capacity_factor*x.capacity]) > 0

    def getTeacherNames(self):
        teachers = []
        for teacher in self.teachers():
            name = '%s %s' % (teacher.first_name,
                              teacher.last_name)
            if name.strip() == '':
                name = teacher.username
            teachers.append(name)
        return teachers

    def getTeacherNamesLast(self):
        teachers = []
        for teacher in self.teachers():
            name = '%s, %s' % (teacher.last_name,
                              teacher.first_name)
            if name.strip() == '':
                name = teacher.username
            teachers.append(name)
        return teachers

    def cannotAdd(self, user, checkFull=True):
        """ Go through and give an error message if this user cannot add this class to their schedule. """
        if not user.isStudent() and not Tag.getTag("allowed_student_types", target=self.parent_program):
            return 'You are not a student!'
        
        blocked_student_types = Tag.getTag("blocked_student_types", target=self)
        if blocked_student_types and not (set(user.getUserTypes()) & set(blocked_student_types.split(","))):
            return "Cannot accept more users of your account type!"
        
        if not self.isAccepted():
            return 'This class is not accepted.'

#        if checkFull and self.parent_program.isFull(use_cache=use_cache) and not ESPUser(user).canRegToFullProgram(self.parent_program):
        if checkFull and self.parent_program.isFull(use_cache=True) and not ESPUser(user).canRegToFullProgram(self.parent_program):
            return 'This program cannot accept any more students!  Please try again in its next session.'

        if checkFull and self.isFull():
            scrmi = self.parent_program.getModuleExtension('StudentClassRegModuleInfo')
            return scrmi.temporarily_full_text

        if not Tag.getTag("allowed_student_types", target=self.parent_program):
            verb_override = GetNode('V/Flags/Registration/GradeOverride')
            if user.getGrade(self.parent_program) < self.grade_min or \
                   user.getGrade(self.parent_program) > self.grade_max:
                if not UserBit.UserHasPerms(user = user,
                                            qsc  = self.anchor,
                                            verb = verb_override):
                    return 'You are not in the requested grade range for this class.'

        # student has no classes...no conflict there.
        if user.getClasses(self.parent_program, verbs=[self.parent_program.getModuleExtension('StudentClassRegModuleInfo').signup_verb.name]).count() == 0:
            return False

        for section in self.get_sections():
            if user.isEnrolledInClass(section):
                return 'You are already signed up for a section of this class!'
        
        res = False
        # check to see if there's a conflict with each section of the subject, or if the user
        # has already signed up for one of the sections of this class
        for section in self.get_sections():
            res = section.cannotAdd(user, checkFull)
            if not res: # if any *can* be added, then return False--we can add this class
                return res

        # res can't have ever been False--so we must have an error. Pass it along.
        return 'This class conflicts with your schedule!'

    def makeTeacher(self, user):
        v = GetNode('V/Flags/Registration/Teacher')
        
        ub, created = UserBit.objects.get_or_create(user = user,
                                qsc = self.anchor,
                                verb = v)
        ub.renew()
        return True

    def removeTeacher(self, user):
        v = GetNode('V/Flags/Registration/Teacher')

        for bit in UserBit.objects.filter(user = user, qsc = self.anchor, verb = v):
            bit.expire()
            
        return True

    def subscribe(self, user):
        v = GetNode('V/Subscribe')

        ub, created = UserBit.objects.get_or_create(user = user,
                                qsc = self.anchor,
                                verb = v)

        return True
    
    def makeAdmin(self, user, endtime = None):
        v = GetNode('V/Administer/Edit')

        ub, created = UserBit.objects.get_or_create(user = user,
                                qsc = self.anchor,
                                verb = v)


        return True        

    def removeAdmin(self, user):
        v = GetNode('V/Administer/Edit')
        UserBit.objects.filter(user = user,
                               qsc = self.anchor,
                               verb = v).delete()
        return True

    def getResourceRequests(self): # get all resource requests associated with this ClassSubject
        from esp.resources.models import ResourceRequest
        return ResourceRequest.objects.filter(target__parent_class=self)

    def conflicts(self, teacher):
        from esp.users.models import ESPUser
        from datetime import timedelta
        user = ESPUser(teacher)
        if user.getTaughtClasses().count() == 0:
            return False
        
        for cls in user.getTaughtClasses().filter(parent_program = self.parent_program):
            for section in cls.get_sections():
                for time in section.meeting_times.all():
                    for sec in self.sections.all().exclude(id=section.id):
                        if sec.meeting_times.filter(id = time.id).count() > 0:
                            return True
                            
                    
        #   Check that adding this teacher as a coteacher would not overcommit them
        #   to more hours of teaching than the program allows.
        avail = Event.collapse(user.getAvailableTimes(self.parent_program, ignore_classes=True), tol=timedelta(minutes=15))
        time_avail = 0.0
        for tg in avail:
            td = tg.end - tg.start
            time_avail += (td.seconds / 3600.0)
        for cls in user.getTaughtClasses(self.parent_program):
            for sec in cls.get_sections():
                time_avail -= float(str(sec.duration))
        time_needed = 0.0
        for sec in self.get_sections():
            time_needed += float(str(sec.duration))
        if time_needed > time_avail:
            return True
            
        return False

    def isAccepted(self): return self.status > 0
    def isReviewed(self): return self.status != 0
    def isRejected(self): return self.status == -10
    def isCancelled(self): return self.status == -20
    isCanceled = isCancelled    # Yay alternative spellings
    
    def isRegOpen(self):
        for sec in self.sections.all():
            if sec.isRegOpen():
                return True
        return False
    
    def isRegClosed(self):
        for sec in self.get_sections():
            if not sec.isRegClosed():
                return False
        return True
        
    def accept(self, user=None, show_message=False):
        """ mark this class as accepted """
        if self.isAccepted():
            return False # already accepted

        self.status = 10
        # I do not understand the following line, but it saves us from "Cannot convert float to Decimal".
        # Also seen in /esp/program/modules/forms/management.py -ageng 2008-11-01
        #self.duration = Decimal(str(self.duration))
        self.save()
        #   Accept any unreviewed sections.
        for sec in self.sections.all():
            if sec.status == 0:
                sec.status = 10
                sec.save()

        if not show_message:
            return True

        subject = 'Your %s class was approved!' % (self.parent_program.niceName())
        
        content =  """Congratulations, your class,
%s,
was approved! Please go to http://esp.mit.edu/teach/%s/class_status/%s to view your class' status.

-esp.mit.edu Autogenerated Message""" % \
                  (self.title(), self.parent_program.getUrlBase(), self.id)
        if user is None:
            user = AnonymousUser()
        Entry.post(user, self.anchor.tree_create(['TeacherEmail']), subject, content, True)       
        return True

    def propose(self):
        """ Mark this class as just `proposed' """
        self.status = 0
        self.save()

    def reject(self):
        """ Mark this class as rejected; also kicks out students from each section. """
        for sec in self.sections.all():
            sec.status = -10
            sec.save()
        self.clearStudents()
        self.status = -10
        self.save()

    def cancel(self, email_students=True, explanation=None):
        """ Cancel this class. Has yet to do anything useful. """
        for sec in self.sections.all():
            sec.cancel(email_students, explanation)
        self.status = -20
        self.save()
        
    def clearStudents(self):
        for sec in self.sections.all():
            sec.clearStudents()
            
    def docs_summary(self):
        """ Return the first three documents associated
        with a class, for previewing. """

        retVal = self.cache['docs_summary']

        if retVal is not None:
            return retVal

        retVal = self.anchor.media_set.all()[:3]
        list(retVal)

        self.cache['docs_summary'] = retVal

        return retVal
            
    def getUrlBase(self):
        """ gets the base url of this class """
        return self.url() # This makes looking up subprograms by name work; I've left it so that it can be undone without too much effort
        tmpnode = self.anchor
        urllist = []
        while tmpnode.name != 'Programs':
            urllist.insert(0,tmpnode.name)
            tmpnode = tmpnode.parent
        return "/".join(urllist)

    def getRegistrations(self, user):
        from esp.program.models import StudentRegistration
        now = datetime.datetime.now()
        return StudentRegistration.objects.filter(section__in=self.sections.all(), user=user, start_date__lte=now, end_date__gte=now).order_by('start_date')
    
    def getRegVerbs(self, user):
        """ Get the list of verbs that a student has within this class's anchor. """
        return self.getRegistrations(user).values_list('relationship__name', flat=True)

    def preregister_student(self, user, overridefull=False, automatic=False):
        """ Register the student for the least full section of the class
        that fits into their schedule. """
        sections = user.getEnrolledSections()
        time_taken = []
        for c in sections:
            time_taken += list(c.meeting_times.all())
        
        best_section = None
        min_ratio = 1.0
        for sec in self.sections.all():
            available = True
            for t in sec.meeting_times.all():
                if t in time_taken:
                    available = False
            if available and (float(sec.num_students()) / (sec.capacity + 1)) < min_ratio:
                min_ratio = float(sec.num_students()) / (sec.capacity + 1)
                best_section = sec
        
        if best_section:
            best_section.preregister_student(user, overridefull, automatic)
            
    def unpreregister_student(self, user):
        """ Find the student's registration for the class and expire it. 
        Also update the cache on each of the sections.  """
        for s in self.sections.all():
            s.unpreregister_student(user)

    def getArchiveClass(self):
        from esp.program.models import ArchiveClass
        
        result = ArchiveClass.objects.filter(original_id=self.id)
        if result.count() > 0:
            return result[0]
        
        result = ArchiveClass()
        date_dir = self.parent_program.anchor.name.split('_')
        result.program = self.parent_program.anchor.parent.name
        result.year = date_dir[0][:4]
        if len(date_dir) > 1:
            result.date = date_dir[1]
        teacher_strs = ['%s %s' % (t.first_name, t.last_name) for t in self.teachers()]
        result.teacher = ' and '.join(teacher_strs)
        result.category = self.category.category[:32]
        result.title = self.title()
        result.description = self.class_info
        if self.prereqs and len(self.prereqs) > 0:
            result.description += '\n\nThe prerequisites for this class were: %s' % self.prereqs
        result.teacher_ids = '|' + '|'.join([str(t.id) for t in self.teachers()]) + '|'
        all_students = self.students()
        result.student_ids = '|' + '|'.join([str(s.id) for s in all_students]) + '|'
        result.original_id = self.id
        
        #   It's good to just keep everything in the archives since they are cheap.
        result.save()
        
        return result
        
    def archive(self, delete=False):
        """ Archive a class to reduce the size of the database. """
        from esp.resources.models import ResourceRequest, ResourceAssignment
        
        #   Ensure that the class has been saved in the archive.
        archived_class = self.getArchiveClass()
        
        #   Delete user bits and resource stuff associated with the class.
        #   (Currently leaving ResourceAssignments alone so that schedules can be viewed.)
        if delete:
            UserBit.objects.filter(qsc=self.anchor).delete()
            ResourceRequest.objects.filter(target_subj=self).delete()
            #   ResourceAssignment.objects.filter(target_subj=self).delete()
            for s in self.sections.all():
                ResourceRequest.objects.filter(target=s).delete()
                #   ResourceAssignment.objects.filter(target=s).delete()
        
        #   This function leaves the actual ClassSubject object, its ClassSections,
        #   and the QSD pages alone.
        return archived_class
        
    def update_cache(self):
        self.teachers(use_cache = False)

    @staticmethod
    def catalog_sort(one, other):
        cmp1 = cmp(one.category.category, other.category.category)
        if cmp1 != 0:
            return cmp1
        cmp2 = ClassSubject.class_sort_by_timeblock(one, other)
        if cmp2 != 0:
            return cmp2
        cmp3 = ClassSubject.class_sort_by_title(one, other)
        if cmp3 != 0:
            return cmp3
        return cmp(one, other)
    
    @staticmethod
    def class_sort_by_category(one, other):
        return cmp(one.category.category, other.category.category)
        
    @staticmethod
    def class_sort_by_id(one, other):
        return cmp(one.id, other.id)

    @staticmethod
    def class_sort_by_teachers(one, other):
        return cmp( sorted(one.getTeacherNames()), sorted(other.getTeacherNames()) )
    
    @staticmethod
    def class_sort_by_title(one, other):
        return cmp(one.title(), other.title())

    @staticmethod
    def class_sort_by_timeblock(one, other):
        if len(one.all_meeting_times) == 0:
            if len(other.all_meeting_times) == 0:
                return 0
            else:
                return -1
        else:
            if len(other.all_meeting_times) == 0:
                return 1
            else:
                return cmp(one.all_meeting_times[0], other.all_meeting_times[0])

    @staticmethod
    def class_sort_noop(one, other):
        return 0

    @staticmethod
    def sort_muxer(sorters):
        def sort_fn(one, other):
            for fn in sorters:
                val = fn(one, other)
                if val != 0:
                    return val
            return 0
        return sort_fn

    def save(self, *args, **kwargs):
        super(ClassSubject, self).save(*args, **kwargs)
        self.update_cache()
        if self.status < 0:
            # Punt teachers all of whose classes have been rejected, from the programwide teachers mailing list
            teachers = self.teachers()
            for t in teachers:
                if ESPUser(t).getTaughtClasses(self.parent_program).filter(status__gte=10).count() == 0:
                    from esp.mailman import remove_list_member
                    mailing_list_name = "%s_%s" % (self.parent_program.anchor.parent.name, self.parent_program.anchor.name)
                    teachers_list_name = "%s-%s" % (mailing_list_name, "teachers")
                    remove_list_member(teachers_list_name, t.email)

    class Meta:
        db_table = 'program_class'
        app_label = 'program'
        

class ClassImplication(models.Model):
    """ Indicates class prerequisites corequisites, and the like """
    cls = models.ForeignKey(ClassSubject, null=True) # parent class
    parent = models.ForeignKey('self', null=True, default=None) # parent classimplication
    is_prereq = models.BooleanField(default=True) # if not a prereq, it's a coreq
    enforce = models.BooleanField(default=True)
    member_ids = models.CommaSeparatedIntegerField(max_length=100, blank=True, null=False) # implied classes (get implied implications with classimplication_set instead)
    operation = models.CharField(max_length=4, choices = ( ('AND', 'All'), ('OR', 'Any'), ('XOR', 'Exactly One') ))

    def member_id_ints_get(self):
        return [ int(s) for s in self.member_ids.split(',') ]

    def member_id_ints_set(self, value):
        self.member_ids = ",".join([ str(n) for n in value ])

    member_id_ints = property( member_id_ints_get, member_id_ints_set )
    
    class Meta:
        verbose_name_plural = 'Class Implications'
        app_label = 'program'
        db_table = 'program_classimplications'
    
    class Admin:
        pass
    
    def __unicode__(self):
        return 'Implications for %s' % self.cls
    
    def _and(lst):
        """ True iff all elements in lst are true """
        for i in lst:
            if not i:
                return False

        return True

    def _or(lst):
        """ True iff at least one element in lst is true """
        for i in lst:
            if i:
                return True

        return False

    def _xor(lst):
        """ True iff lst contains exactly one true element """
        true_count = 0

        for i in lst:
            if i:
                true_count += 1

            if true_count > 1:
                return False

        if true_count == 1:
            return True
        else:
            return False
            
    _ops = { 'AND': _and, 'OR': _or, 'XOR': _xor }

    def fails_implication(self, student, already_seen_implications=set(), without_classes=set()):
        """ Returns either False, or the ClassImplication that fails (may be self, may be a subimplication) """
        class_set = ClassSubject.objects.filter(id__in=self.member_id_ints)
        
        class_valid_iterator = [ (student in c.students(False) and c.id not in without_classes) for c in class_set ]
        subimplication_valid_iterator = [ (not i.fails_implication(student, already_seen_implications, without_classes)) for i in self.classimplication_set.all() ]
        
        if not ClassImplication._ops[self.operation](class_valid_iterator + subimplication_valid_iterator):
            return self
        else:
            return False


class ClassCategories(models.Model):
    """ A list of all possible categories for an ESP class

    Categories include 'Mathematics', 'Science', 'Zocial Zciences', etc.
    """
    
    category = models.TextField(blank=False)
    symbol = models.CharField(max_length=1, default='?', blank=False)
    seq = models.IntegerField(default=0)
    
    class Meta:
        verbose_name_plural = 'Class Categories'
        app_label = 'program'
        db_table = 'program_classcategories'

    def __unicode__(self):
        return u'%s (%s)' % (self.category, self.symbol)
        
        
    @staticmethod
    def category_string(letter):
        
        results = ClassCategories.objects.filter(category__startswith = letter)
        
        if results.count() == 1:
            return results[0].category
        else:
            return None

    class Admin:
        pass

def open_class_category():
    return ClassCategories.objects.get_or_create(category='Walk-in Seminar', symbol='W', seq=0)[0]

@cache_function
def sections_in_program_by_id(prog):
    return [int(x) for x in ClassSection.objects.filter(parent_class__parent_program=prog).distinct().values_list('id', flat=True)]
sections_in_program_by_id.depend_on_model(ClassSection)
sections_in_program_by_id.depend_on_model(ClassSubject)

def install():
    """ Initialize the default class categories. """
    category_dict = {
        'S': 'Science',
        'M': 'Math & Computer Science',
        'E': 'Engineering',
        'A': 'Arts',
        'H': 'Humanities',
        'X': 'Miscellaneous',
    }
    
    for key in category_dict:
        cat = ClassCategories()
        cat.symbol = key
        cat.category = category_dict[key]
        cat.save()
