package attendance

import (
	"errors"
	"fmt"
	"sort"
	"sync"
	"time"
)

var (
	ErrUnknownEmployee = errors.New("unknown employee")
	ErrLowConfidence   = errors.New("recognition confidence below threshold")
)

// Service keeps employees and camera events in memory and computes attendance from them.
// Swap the maps for a database once the data model settles.
type Service struct {
	mu            sync.RWMutex
	employees     map[string]Employee
	events        map[string][]Event // keyed by employee ID
	loc           *time.Location
	minConfidence float64
	now           func() time.Time
}

func NewService(loc *time.Location, minConfidence float64) *Service {
	return &Service{
		employees:     map[string]Employee{},
		events:        map[string][]Event{},
		loc:           loc,
		minConfidence: minConfidence,
		now:           time.Now,
	}
}

func (s *Service) Location() *time.Location { return s.loc }

func (s *Service) UpsertEmployee(e Employee) error {
	if err := e.Shift.validate(); err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.employees[e.ID] = e
	return nil
}

func (s *Service) Employees() []Employee {
	s.mu.RLock()
	defer s.mu.RUnlock()
	list := make([]Employee, 0, len(s.employees))
	for _, e := range s.employees {
		list = append(list, e)
	}
	sort.Slice(list, func(i, j int) bool { return list[i].ID < list[j].ID })
	return list
}

func (s *Service) RecordEvent(ev Event) error {
	if ev.Confidence < s.minConfidence {
		return fmt.Errorf("%w: %.2f < %.2f", ErrLowConfidence, ev.Confidence, s.minConfidence)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.employees[ev.EmployeeID]; !ok {
		return fmt.Errorf("%w: %s", ErrUnknownEmployee, ev.EmployeeID)
	}
	s.events[ev.EmployeeID] = append(s.events[ev.EmployeeID], ev)
	return nil
}

// DailyReport computes the attendance of every employee for the given day.
func (s *Service) DailyReport(day time.Time) []Record {
	day = day.In(s.loc)
	dayStart := time.Date(day.Year(), day.Month(), day.Day(), 0, 0, 0, 0, s.loc)
	dayEnd := dayStart.AddDate(0, 0, 1)
	now := s.now().In(s.loc)

	s.mu.RLock()
	defer s.mu.RUnlock()

	records := make([]Record, 0, len(s.employees))
	for _, e := range s.employees {
		rec := Record{EmployeeID: e.ID, Name: e.Name, Date: dayStart.Format("2006-01-02")}

		for _, ev := range s.events[e.ID] {
			t := ev.Timestamp.In(s.loc)
			if t.Before(dayStart) || !t.Before(dayEnd) {
				continue
			}
			if rec.FirstSeen == nil || t.Before(*rec.FirstSeen) {
				first := t
				rec.FirstSeen = &first
			}
			if rec.LastSeen == nil || t.After(*rec.LastSeen) {
				last := t
				rec.LastSeen = &last
			}
		}

		shiftStart := at(dayStart, e.Shift.Start)
		deadline := shiftStart.Add(time.Duration(e.Shift.GraceMinutes) * time.Minute)
		switch {
		case !e.Shift.worksOn(dayStart.Weekday()):
			rec.Status = StatusDayOff
		case rec.FirstSeen == nil && now.Before(at(dayStart, e.Shift.End)):
			rec.Status = StatusPending
		case rec.FirstSeen == nil:
			rec.Status = StatusAbsent
		case rec.FirstSeen.After(deadline):
			rec.Status = StatusLate
			rec.LateMinutes = int(rec.FirstSeen.Sub(shiftStart).Minutes())
		default:
			rec.Status = StatusOnTime
		}
		records = append(records, rec)
	}
	sort.Slice(records, func(i, j int) bool { return records[i].EmployeeID < records[j].EmployeeID })
	return records
}
