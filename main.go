package main

import (
	"KaibaDecode/attendance"
	"fmt"
	"github.com/gin-gonic/gin"
	"io"
	"log"
	"net/http"
	"os"
	"strconv"
	"time"
	_ "time/tzdata"
)

func main() {
	gin.SetMode(gin.ReleaseMode)
	r := setupRouter()
	attendance.RegisterRoutes(r.Group("/api/v1"), newAttendanceService())
	log.Fatal(r.Run(":8080"))
}

func setupRouter() *gin.Engine {
	router := gin.New()

	// LoggerWithFormatter middleware will write the logs to gin.DefaultWriter
	// By default gin.DefaultWriter = os.Stdout
	router.Use(gin.LoggerWithFormatter(func(param gin.LogFormatterParams) string {
		// your custom format
		return fmt.Sprintf("%s - [%s] \"%s %s %s %d %s \"%s\" %s\"\n",
			param.ClientIP,
			param.TimeStamp.Format(time.RFC1123),
			param.Method,
			param.Path,
			param.Request.Proto,
			param.StatusCode,
			param.Latency,
			param.Request.UserAgent(),
			param.ErrorMessage,
		)
	}))
	router.Use(gin.Recovery())

	router.GET("/ping", func(c *gin.Context) {
		c.String(http.StatusOK, "pong")
	})

	// Set a lower memory limit for multipart forms (default is 32 MiB)
	router.MaxMultipartMemory = 8 << 20 // 8 MiB
	router.POST("/upload", handleUpload)

	return router
}

// newAttendanceService reads ATTENDANCE_TZ (default Asia/Ho_Chi_Minh) and
// ATTENDANCE_MIN_CONFIDENCE (default 0.6) from the environment.
func newAttendanceService() *attendance.Service {
	tz := os.Getenv("ATTENDANCE_TZ")
	if tz == "" {
		tz = "Asia/Ho_Chi_Minh"
	}
	loc, err := time.LoadLocation(tz)
	if err != nil {
		log.Fatalf("invalid ATTENDANCE_TZ: %v", err)
	}
	minConfidence := 0.6
	if v := os.Getenv("ATTENDANCE_MIN_CONFIDENCE"); v != "" {
		if minConfidence, err = strconv.ParseFloat(v, 64); err != nil {
			log.Fatalf("invalid ATTENDANCE_MIN_CONFIDENCE: %v", err)
		}
	}
	return attendance.NewService(loc, minConfidence)
}

func decode(file []byte, cipher string) (string, error) {
	return "decode success", nil
}
func handleUpload(c *gin.Context) {
	// Single file
	file, _ := c.FormFile("file")
	cipher := c.PostForm("cipher")

	// Upload the file to specific dst.
	if file == nil || len(cipher) <= 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "Missing file or cipher key"})
		c.AbortWithStatus(http.StatusBadRequest)
		return
	}

	src, _ := file.Open()
	defer src.Close()

	data, err := io.ReadAll(src)

	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		c.AbortWithStatus(http.StatusInternalServerError)
	}

	metaStr, err := decode(data, cipher)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		c.AbortWithStatus(http.StatusInternalServerError)
	}
	c.JSON(http.StatusOK, gin.H{"result": metaStr})
}
