/*
Copyright 2022 Google LLC

 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at

      http://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
*/
// Package storage provides functionality to interact with permanent storage.
package storage

import (
	"context"
	"fmt"
	"sync"
	"time"

	"cloud.google.com/go/datastore"
	"github.com/go-git/go-git/v5/plumbing"
	"github.com/google/osv.dev/docker/indexer/stages/preparation"
	"github.com/google/osv.dev/docker/indexer/stages/processing"
)

const (
	docKind    = "RepoIndex"
	resultKind = "RepoIndexBucket"
	treeKind   = "RepoIndexResultTree"
	// Address-HashType-CommitHash
	docKeyFmt = "%s-%s-%x"
	// BucketHash-HashType
	resultKeyFmt = "%x-%s"
	// NodeHash-HashType-FilesContained-Height
	treeKeyFmt = "%x-%s-%d-%d"
	pageSize   = 1000
)

// document represents a single repository entry in datastore.
type document struct {
	Name         string    `datastore:"name"`
	BaseCPE      string    `datastore:"base_cpe"`
	Version      string    `datastore:"version"`
	Commit       []byte    `datastore:"commit"`
	Tag          string    `datastore:"tag"`
	When         time.Time `datastore:"when,omitempty"`
	RepoType     string    `datastore:"repo_type"`
	RepoAddr     string    `datastore:"repo_addr"`
	FileExts     []string  `datastore:"file_exts"`
	FileHashType string    `datastore:"file_hash_type"`
}

type result struct {
	BucketHash []byte   `datastore:"bucket_hash"`
	Path       []string `datastore:"bucket_results.path,noindex"`
	Hash       [][]byte `datastore:"bucket_results.hash,noindex"`
}

func newDoc(repoInfo *preparation.Result, hashType string, bucketResults [][]*processing.FileResult, baseTreeLayer []*processing.TreeNode) (*document, []*result) {
	doc := &document{
		Name:         repoInfo.Name,
		BaseCPE:      repoInfo.BaseCPE,
		Version:      repoInfo.Version,
		Commit:       repoInfo.Commit[:],
		Tag:          repoInfo.CommitTag,
		When:         repoInfo.When,
		RepoType:     repoInfo.Type,
		RepoAddr:     repoInfo.Addr,
		FileExts:     repoInfo.FileExts,
		FileHashType: hashType,
	}
	result := []*result{}
	for i, v := range bucketResults {
		if len(bucketResults) == 0 {
			continue
		}
		result = append(result, newResult(v, baseTreeLayer[i].NodeHash))
	}
	return doc, result

}

func newResult(results []*processing.FileResult, bucketHash []byte) *result {
	var (
		paths  []string
		hashes [][]byte
	)

	for _, r := range results {
		paths = append(paths, r.Path)
		hashes = append(hashes, r.Hash)
	}
	return &result{Path: paths, Hash: hashes, BucketHash: bucketHash}
}

// Store provides the functionality to check for existing documents
// in datastore and add new ones.
type Store struct {
	dsCl  *datastore.Client
	cache sync.Map
}

// New returns a new Store.
func New(ctx context.Context, projectID string) (*Store, error) {
	client, err := datastore.NewClient(ctx, projectID)
	if err != nil {
		return nil, err
	}
	return &Store{dsCl: client, cache: sync.Map{}}, nil
}

// Exists checks whether a name/hash pair already exists in datastore.
func (s *Store) Exists(ctx context.Context, addr string, hashType string, hash plumbing.Hash) (bool, error) {
	if _, ok := s.cache.Load(fmt.Sprintf(docKeyFmt, addr, hashType, hash)); ok {
		return true, nil
	}
	key := datastore.NameKey(docKind, fmt.Sprintf(docKeyFmt, addr, hashType, hash), nil)
	tmp := &document{}
	if err := s.dsCl.Get(ctx, key, tmp); err != nil {
		if err == datastore.ErrNoSuchEntity {
			return false, nil
		}
		return false, err
	}
	s.cache.Store(fmt.Sprintf(docKeyFmt, addr, hashType, hash), true)
	return true, nil
}

// Store stores a new entry in datastore.
func (s *Store) Store(ctx context.Context, repoInfo *preparation.Result, hashType string, bucketResults [][]*processing.FileResult, treeNodes [][]*processing.TreeNode) error {
	docKey := datastore.NameKey(docKind, fmt.Sprintf(docKeyFmt, repoInfo.Addr, hashType, repoInfo.Commit[:]), nil)
	doc, results := newDoc(repoInfo, hashType, bucketResults, treeNodes[0])
	_, err := s.dsCl.RunInTransaction(ctx, func(tx *datastore.Transaction) error {
		_, err := tx.Put(docKey, doc)
		if err != nil {
			return err
		}
		for _, r := range results {
			resultKey := datastore.NameKey(resultKind, fmt.Sprintf(resultKeyFmt, r.BucketHash, hashType), docKey)
			_, err := tx.Put(resultKey, r)
			if err != nil {
				return err
			}
		}

		for _, layer := range treeNodes {
			putMultiKeys := []*datastore.Key{}
			putMultiNodes := []*processing.TreeNode{}
			for _, node := range layer {
				if node.FilesContained == 0 {
					continue
				}

				treeKey := datastore.NameKey(treeKind,
					fmt.Sprintf(treeKeyFmt, node.NodeHash, hashType, node.FilesContained, node.Height),
					docKey)

				putMultiKeys = append(putMultiKeys, treeKey)
				putMultiNodes = append(putMultiNodes, node)
			}
			_, err := tx.PutMulti(putMultiKeys, putMultiNodes)
			if err != nil {
				return err
			}
		}
		return nil
	})
	return err
}

// Close closes the datastore client.
func (s *Store) Close() {
	s.dsCl.Close()
}
