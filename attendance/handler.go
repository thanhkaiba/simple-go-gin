package attendance

import (
	"errors"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
)

// RegisterRoutes mounts the attendance API on the given router group.
//
//	POST /employees            create or update an employee and their shift
//	GET  /employees            list employees
//	POST /events               camera / face recognition webhook
//	GET  /attendance?date=...  daily report, optional &status=late|absent|...
func RegisterRoutes(rg *gin.RouterGroup, svc *Service) {
	rg.POST("/employees", func(c *gin.Context) {
		var e Employee
		if err := c.ShouldBindJSON(&e); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		if err := svc.UpsertEmployee(e); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		c.JSON(http.StatusOK, e)
	})

	rg.GET("/employees", func(c *gin.Context) {
		c.JSON(http.StatusOK, svc.Employees())
	})

	rg.POST("/events", func(c *gin.Context) {
		var ev Event
		if err := c.ShouldBindJSON(&ev); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		if err := svc.RecordEvent(ev); err != nil {
			status := http.StatusInternalServerError
			if errors.Is(err, ErrUnknownEmployee) {
				status = http.StatusNotFound
			} else if errors.Is(err, ErrLowConfidence) {
				status = http.StatusUnprocessableEntity
			}
			c.JSON(status, gin.H{"error": err.Error()})
			return
		}
		c.JSON(http.StatusAccepted, gin.H{"result": "recorded"})
	})

	rg.GET("/attendance", func(c *gin.Context) {
		day := time.Now().In(svc.Location())
		if d := c.Query("date"); d != "" {
			parsed, err := time.ParseInLocation("2006-01-02", d, svc.Location())
			if err != nil {
				c.JSON(http.StatusBadRequest, gin.H{"error": "date must be YYYY-MM-DD"})
				return
			}
			day = parsed
		}

		records := svc.DailyReport(day)
		if status := c.Query("status"); status != "" {
			filtered := records[:0]
			for _, r := range records {
				if string(r.Status) == status {
					filtered = append(filtered, r)
				}
			}
			records = filtered
		}
		c.JSON(http.StatusOK, records)
	})
}
