package attendance

import (
	"fmt"
	"time"
)

// Shift describes the working hours an employee is expected to follow.
// Start and End use "HH:MM" in the service's time zone.
type Shift struct {
	Start        string         `json:"start" binding:"required"`
	End          string         `json:"end" binding:"required"`
	GraceMinutes int            `json:"grace_minutes"`
	WorkDays     []time.Weekday `json:"work_days"` // empty means Monday to Friday
}

// Employee is a person the cameras can recognize.
type Employee struct {
	ID    string `json:"id" binding:"required"`
	Name  string `json:"name" binding:"required"`
	Shift Shift  `json:"shift"`
}

// Event is a single recognition reported by a camera or the face recognition service.
type Event struct {
	EmployeeID string    `json:"employee_id" binding:"required"`
	CameraID   string    `json:"camera_id"`
	Timestamp  time.Time `json:"timestamp" binding:"required"`
	Confidence float64   `json:"confidence"`
}

type Status string

const (
	StatusOnTime  Status = "on_time"
	StatusLate    Status = "late"
	StatusAbsent  Status = "absent"
	StatusPending Status = "pending" // not seen yet, but the shift has not ended
	StatusDayOff  Status = "day_off"
)

// Record is the computed attendance of one employee on one day.
type Record struct {
	EmployeeID  string     `json:"employee_id"`
	Name        string     `json:"name"`
	Date        string     `json:"date"`
	Status      Status     `json:"status"`
	FirstSeen   *time.Time `json:"first_seen,omitempty"`
	LastSeen    *time.Time `json:"last_seen,omitempty"`
	LateMinutes int        `json:"late_minutes,omitempty"`
}

func (s Shift) worksOn(d time.Weekday) bool {
	if len(s.WorkDays) == 0 {
		return d >= time.Monday && d <= time.Friday
	}
	for _, w := range s.WorkDays {
		if w == d {
			return true
		}
	}
	return false
}

func (s Shift) validate() error {
	start, err := parseClock(s.Start)
	if err != nil {
		return err
	}
	end, err := parseClock(s.End)
	if err != nil {
		return err
	}
	if end <= start {
		return fmt.Errorf("shift end %q must be after start %q", s.End, s.Start)
	}
	if s.GraceMinutes < 0 {
		return fmt.Errorf("grace_minutes must not be negative")
	}
	return nil
}

// at returns the absolute time of a "HH:MM" clock value on the given day.
func at(day time.Time, clock string) time.Time {
	d, _ := parseClock(clock)
	y, m, dd := day.Date()
	return time.Date(y, m, dd, 0, 0, 0, 0, day.Location()).Add(d)
}

func parseClock(clock string) (time.Duration, error) {
	t, err := time.Parse("15:04", clock)
	if err != nil {
		return 0, fmt.Errorf("invalid time %q, expected HH:MM", clock)
	}
	return time.Duration(t.Hour())*time.Hour + time.Duration(t.Minute())*time.Minute, nil
}
