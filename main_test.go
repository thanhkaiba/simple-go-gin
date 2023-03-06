package main

import (
	"bytes"
	"github.com/stretchr/testify/assert"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
)

func TestPingRoute(t *testing.T) {
	router := setupRouter()

	w := httptest.NewRecorder()
	req, _ := http.NewRequest(http.MethodGet, "/ping", nil)
	router.ServeHTTP(w, req)

	assert.Equal(t, http.StatusOK, w.Code)
	assert.Equal(t, "pong", w.Body.String())
}

func TestUploadFail(t *testing.T) {
	router := setupRouter()

	w := httptest.NewRecorder()
	req, _ := http.NewRequest(http.MethodPost, "/upload", nil)
	router.ServeHTTP(w, req)

	assert.Equal(t, http.StatusBadRequest, w.Code)
	assert.Equal(t, "{\"error\":\"Missing file or cipher key\"}", w.Body.String())
}

func TestDecode(t *testing.T) {
	router := setupRouter()

	path := ".\\main_test.go"

	body := new(bytes.Buffer)
	writer := multipart.NewWriter(body)
	part, err := writer.CreateFormFile("file", path)
	assert.NoError(t, err)
	sample, err := os.Open(path)
	assert.NoError(t, err)

	_, err = io.Copy(part, sample)
	assert.NoError(t, err)

	err = writer.WriteField("cipher", "123456")
	assert.NoError(t, err)

	assert.NoError(t, writer.Close())

	w := httptest.NewRecorder()
	req, _ := http.NewRequest(http.MethodPost, "/upload", body)
	req.Header.Set("Content-Type", writer.FormDataContentType())
	router.ServeHTTP(w, req)

	assert.Equal(t, http.StatusOK, w.Code)
	assert.Equal(t, "{\"result\":\"decode success\"}", w.Body.String())
}
