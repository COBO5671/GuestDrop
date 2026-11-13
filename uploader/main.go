package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

var uploadIDPattern = regexp.MustCompile(`^[0-9a-f-]{36}$`)

type uploadInfo struct {
	ID            string `json:"id"`
	Size          int64  `json:"size"`
	Received      int64  `json:"received"`
	Status        string `json:"status"`
	Active        int    `json:"active"`
	MaxChunkBytes int64  `json:"maxChunkBytes"`
}

type server struct {
	appURL    string
	token     string
	uploadDir string
	client    *http.Client
	mu        sync.Mutex
	locks     map[string]*sync.Mutex
}

func main() {
	healthcheck := flag.Bool("healthcheck", false, "check the running uploader")
	flag.Parse()
	if *healthcheck {
		response, err := (&http.Client{Timeout: 3 * time.Second}).Get("http://127.0.0.1:8090/health")
		if err != nil || response.StatusCode != http.StatusOK {
			os.Exit(1)
		}
		_ = response.Body.Close()
		return
	}

	token := os.Getenv("INTERNAL_UPLOAD_TOKEN")
	if token == "" {
		token = os.Getenv("SECRET_KEY")
	}
	if token == "" {
		log.Fatal("INTERNAL_UPLOAD_TOKEN or SECRET_KEY is required")
	}
	s := &server{
		appURL:    strings.TrimRight(envOr("APP_INTERNAL_URL", "http://app:8081"), "/"),
		token:     token,
		uploadDir: envOr("UPLOAD_DIR", "/uploads"),
		client:    &http.Client{Timeout: 15 * time.Second},
		locks:     map[string]*sync.Mutex{},
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, _ *http.Request) { writeJSON(w, http.StatusOK, map[string]any{"ok": true}) })
	mux.HandleFunc("POST /api/uploads/{id}/chunk", s.uploadChunk)
	log.Println("GuestDrop native uploader listening on :8090")
	log.Fatal((&http.Server{Addr: ":8090", Handler: mux, ReadHeaderTimeout: 10 * time.Second, IdleTimeout: 2 * time.Minute}).ListenAndServe())
}

func envOr(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

func (s *server) uploadLock(id string) *sync.Mutex {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.locks[id] == nil {
		s.locks[id] = &sync.Mutex{}
	}
	return s.locks[id]
}

func (s *server) uploadChunk(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !uploadIDPattern.MatchString(id) {
		writeError(w, http.StatusNotFound, "upload not found")
		return
	}
	offset, err := strconv.ParseInt(r.URL.Query().Get("offset"), 10, 64)
	if err != nil || offset < 0 {
		writeError(w, http.StatusBadRequest, "offset is required")
		return
	}

	lock := s.uploadLock(id)
	lock.Lock()
	defer lock.Unlock()

	info, status, err := s.getInfo(id)
	if err != nil {
		writeError(w, status, err.Error())
		return
	}
	if info.Active != 1 {
		writeError(w, http.StatusForbidden, "event disabled")
		return
	}
	if info.Status == "ready" || info.Status == "importing" || info.Status == "complete" {
		writeJSON(w, http.StatusOK, map[string]any{"received": info.Received, "status": info.Status})
		return
	}
	if offset != info.Received {
		writeJSON(w, http.StatusConflict, map[string]any{"error": "offset mismatch", "received": info.Received})
		return
	}
	if r.ContentLength == 0 {
		writeError(w, http.StatusBadRequest, "empty chunk")
		return
	}
	if r.ContentLength > info.MaxChunkBytes {
		writeError(w, http.StatusRequestEntityTooLarge, "chunk too large")
		return
	}

	path := filepath.Join(s.uploadDir, id+".part")
	file, err := os.OpenFile(path, os.O_RDWR, 0600)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "staged file is unavailable")
		return
	}
	defer file.Close()
	stat, err := file.Stat()
	if err != nil || stat.Size() < offset {
		writeError(w, http.StatusConflict, "staged file is shorter than the saved offset")
		return
	}
	if stat.Size() > offset {
		if err := file.Truncate(offset); err != nil {
			writeError(w, http.StatusInternalServerError, "could not reconcile staged file")
			return
		}
	}
	if _, err := file.Seek(offset, io.SeekStart); err != nil {
		writeError(w, http.StatusInternalServerError, "could not seek staged file")
		return
	}

	limit := info.MaxChunkBytes
	if remaining := info.Size - offset; remaining < limit {
		limit = remaining
	}
	written, copyErr := io.Copy(file, io.LimitReader(r.Body, limit+1))
	if copyErr != nil || written <= 0 || written > limit || (r.ContentLength >= 0 && written != r.ContentLength) {
		_ = file.Truncate(offset)
		writeError(w, http.StatusBadRequest, "incomplete chunk; retrying is safe")
		return
	}
	if err := file.Sync(); err != nil {
		_ = file.Truncate(offset)
		writeError(w, http.StatusInternalServerError, "could not sync staged file")
		return
	}

	body, status, err := s.commit(id, offset, written)
	if err != nil {
		_ = file.Truncate(offset)
		writeError(w, status, err.Error())
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(body)
}

func (s *server) getInfo(id string) (uploadInfo, int, error) {
	var info uploadInfo
	request, _ := http.NewRequest(http.MethodGet, s.appURL+"/internal/uploads/"+id, nil)
	request.Header.Set("X-GuestDrop-Internal", s.token)
	response, err := s.client.Do(request)
	if err != nil {
		return info, http.StatusBadGateway, errors.New("upload metadata service unavailable")
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return info, response.StatusCode, fmt.Errorf("upload metadata request failed (%d)", response.StatusCode)
	}
	if err := json.NewDecoder(response.Body).Decode(&info); err != nil {
		return info, http.StatusBadGateway, errors.New("invalid upload metadata response")
	}
	return info, http.StatusOK, nil
}

func (s *server) commit(id string, offset, written int64) ([]byte, int, error) {
	payload, _ := json.Marshal(map[string]int64{"offset": offset, "written": written})
	request, _ := http.NewRequest(http.MethodPost, s.appURL+"/internal/uploads/"+id+"/commit", bytes.NewReader(payload))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("X-GuestDrop-Internal", s.token)
	response, err := s.client.Do(request)
	if err != nil {
		return nil, http.StatusBadGateway, errors.New("upload commit service unavailable")
	}
	defer response.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(response.Body, 64*1024))
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return nil, response.StatusCode, fmt.Errorf("upload commit failed (%d)", response.StatusCode)
	}
	return body, response.StatusCode, nil
}

func writeError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]any{"error": message})
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
