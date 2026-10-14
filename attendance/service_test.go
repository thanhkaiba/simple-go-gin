package attendance

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/assert"
)

var hcm = time.FixedZone("ICT", 7*3600)

func newTestService(t *testing.T, now time.Time) *Service {
	svc := NewService(hcm, 0.6)
	svc.now = func() time.Time { return now }
	shift := Shift{Start: "08:00", End: "17:00", GraceMinutes: 5}
	for _, e := range []Employee{
		{ID: "e1", Name: "An", Shift: shift},
		{ID: "e2", Name: "Binh", Shift: shift},
		{ID: "e3", Name: "Chi", Shift: shift},
	} {
		if !assert.NoError(t, svc.UpsertEmployee(e)) {
			t.FailNow()
		}
	}
	return svc
}

func byID(records []Record) map[string]Record {
	m := map[string]Record{}
	for _, r := range records {
		m[r.EmployeeID] = r
	}
	return m
}

func TestDailyReport(t *testing.T) {
	// Wednesday 2026-10-14
	day := time.Date(2026, 10, 14, 0, 0, 0, 0, hcm)
	svc := newTestService(t, day.Add(18*time.Hour))

	assert.NoError(t, svc.RecordEvent(Event{EmployeeID: "e1", Timestamp: day.Add(8*time.Hour + 4*time.Minute), Confidence: 0.9}))
	assert.NoError(t, svc.RecordEvent(Event{EmployeeID: "e1", Timestamp: day.Add(17*time.Hour + 30*time.Minute), Confidence: 0.9}))
	assert.NoError(t, svc.RecordEvent(Event{EmployeeID: "e2", Timestamp: day.Add(8*time.Hour + 20*time.Minute), Confidence: 0.9}))
	// Previous day's event must not count.
	assert.NoError(t, svc.RecordEvent(Event{EmployeeID: "e3", Timestamp: day.Add(-time.Hour), Confidence: 0.9}))

	r := byID(svc.DailyReport(day))
	assert.Equal(t, StatusOnTime, r["e1"].Status)
	assert.Equal(t, day.Add(17*time.Hour+30*time.Minute), *r["e1"].LastSeen)
	assert.Equal(t, StatusLate, r["e2"].Status)
	assert.Equal(t, 20, r["e2"].LateMinutes)
	assert.Equal(t, StatusAbsent, r["e3"].Status)
}

func TestDailyReportPendingAndDayOff(t *testing.T) {
	day := time.Date(2026, 10, 14, 0, 0, 0, 0, hcm)
	svc := newTestService(t, day.Add(9*time.Hour))
	assert.Equal(t, StatusPending, byID(svc.DailyReport(day))["e1"].Status)

	saturday := time.Date(2026, 10, 17, 0, 0, 0, 0, hcm)
	assert.Equal(t, StatusDayOff, byID(svc.DailyReport(saturday))["e1"].Status)
}

func TestRecordEventRejects(t *testing.T) {
	svc := newTestService(t, time.Now())
	assert.ErrorIs(t, svc.RecordEvent(Event{EmployeeID: "nobody", Timestamp: time.Now(), Confidence: 0.9}), ErrUnknownEmployee)
	assert.ErrorIs(t, svc.RecordEvent(Event{EmployeeID: "e1", Timestamp: time.Now(), Confidence: 0.3}), ErrLowConfidence)
}

func TestUpsertEmployeeValidatesShift(t *testing.T) {
	svc := NewService(hcm, 0)
	assert.Error(t, svc.UpsertEmployee(Employee{ID: "x", Name: "X", Shift: Shift{Start: "17:00", End: "08:00"}}))
	assert.Error(t, svc.UpsertEmployee(Employee{ID: "x", Name: "X", Shift: Shift{Start: "8h", End: "17:00"}}))
}

func TestHTTPFlow(t *testing.T) {
	gin.SetMode(gin.TestMode)
	day := time.Date(2026, 10, 14, 0, 0, 0, 0, hcm)
	svc := newTestService(t, day.Add(18*time.Hour))
	router := gin.New()
	RegisterRoutes(router.Group("/api/v1"), svc)

	post := func(path string, body any) *httptest.ResponseRecorder {
		b, _ := json.Marshal(body)
		w := httptest.NewRecorder()
		req, _ := http.NewRequest(http.MethodPost, path, bytes.NewReader(b))
		req.Header.Set("Content-Type", "application/json")
		router.ServeHTTP(w, req)
		return w
	}

	w := post("/api/v1/events", Event{EmployeeID: "e2", CameraID: "gate-1", Timestamp: day.Add(9 * time.Hour), Confidence: 0.95})
	assert.Equal(t, http.StatusAccepted, w.Code)
	assert.Equal(t, http.StatusNotFound, post("/api/v1/events", Event{EmployeeID: "zz", Timestamp: day, Confidence: 1}).Code)
	assert.Equal(t, http.StatusUnprocessableEntity, post("/api/v1/events", Event{EmployeeID: "e1", Timestamp: day, Confidence: 0.1}).Code)

	w = httptest.NewRecorder()
	req, _ := http.NewRequest(http.MethodGet, "/api/v1/attendance?date=2026-10-14&status=late", nil)
	router.ServeHTTP(w, req)
	if !assert.Equal(t, http.StatusOK, w.Code) {
		t.FailNow()
	}
	var records []Record
	assert.NoError(t, json.Unmarshal(w.Body.Bytes(), &records))
	if !assert.Len(t, records, 1) {
		t.FailNow()
	}
	assert.Equal(t, "e2", records[0].EmployeeID)
	assert.Equal(t, 60, records[0].LateMinutes)
}
