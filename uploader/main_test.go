package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
)

func TestChunkedRequestCommitsExactOffset(t *testing.T) {
	const id = "11111111-1111-1111-1111-111111111111"
	temp := t.TempDir()
	path := filepath.Join(temp, id+".part")
	if err := os.WriteFile(path, nil, 0600); err != nil {
		t.Fatal(err)
	}
	var committed int64
	app := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-GuestDrop-Internal") != "test-secret" {
			http.Error(w, "unauthorized", http.StatusNotFound)
			return
		}
		if r.Method == http.MethodGet {
			_ = json.NewEncoder(w).Encode(uploadInfo{ID: id, Size: 10 << 20, Received: 0, Status: "uploading", Active: 1, MaxChunkBytes: 5 << 20})
			return
		}
		var body map[string]int64
		_ = json.NewDecoder(r.Body).Decode(&body)
		committed = body["written"]
		_ = json.NewEncoder(w).Encode(map[string]any{"received": committed, "status": "uploading"})
	}))
	defer app.Close()

	s := &server{appURL: app.URL, token: "test-secret", uploadDir: temp, client: app.Client(), locks: map[string]*sync.Mutex{}}
	body := strings.Repeat("x", 5<<20)
	request := httptest.NewRequest(http.MethodPost, "/api/uploads/"+id+"/chunk?offset=0", strings.NewReader(body))
	request.SetPathValue("id", id)
	request.ContentLength = -1 // Exercise HTTP Transfer-Encoding: chunked semantics.
	response := httptest.NewRecorder()
	s.uploadChunk(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", response.Code, response.Body.String())
	}
	if committed != 5<<20 {
		t.Fatalf("committed %s bytes", strconv.FormatInt(committed, 10))
	}
	stat, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if stat.Size() != 5<<20 {
		t.Fatalf("staged size=%d", stat.Size())
	}
}
