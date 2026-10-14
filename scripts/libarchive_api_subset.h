/*
 * Small public-ABI declaration subset for the system libarchive API.
 *
 * Source: libarchive/libarchive v3.7.4, libarchive/archive.h and archive_entry.h
 * (BSD 2-clause; upstream copyright and disclaimer retained below).
 * This file contains declarations only; Apple supplies the implementation
 * as /usr/lib/libarchive.2.dylib. Confirm the linked SDK stub before use.
 */
/*-
 * Copyright (c) 2003-2010 Tim Kientzle
 * All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions
 * are met:
 * 1. Redistributions of source code must retain the above copyright
 *    notice, this list of conditions and the following disclaimer.
 * 2. Redistributions in binary form must reproduce the above copyright
 *    notice, this list of conditions and the following disclaimer in the
 *    documentation and/or other materials provided with the distribution.
 *
 * THIS SOFTWARE IS PROVIDED BY THE AUTHOR(S) ``AS IS'' AND ANY EXPRESS OR
 * IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES
 * OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.
 * IN NO EVENT SHALL THE AUTHOR(S) BE LIABLE FOR ANY DIRECT, INDIRECT,
 * INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT
 * NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
 * DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
 * THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
 * (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF
 * THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 */
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <sys/stat.h>
#include <sys/types.h>

struct archive;
struct archive_entry;
using la_int64_t = int64_t;
using la_ssize_t = ssize_t;

extern "C" {
int archive_version_number(void);
const char* archive_version_string(void);
struct archive* archive_read_new(void);
int archive_read_support_filter_none(struct archive*);
int archive_read_support_format_zip(struct archive*);
int archive_read_open_filename(struct archive*, const char*, size_t);
int archive_read_open_fd(struct archive*, int, size_t);
int archive_read_next_header(struct archive*, struct archive_entry**);
la_ssize_t archive_read_data(struct archive*, void*, size_t);
int archive_read_has_encrypted_entries(struct archive*);
int archive_read_close(struct archive*);
int archive_read_free(struct archive*);

int archive_entry_is_encrypted(struct archive_entry*);
const char* archive_entry_pathname_utf8(struct archive_entry*);
mode_t archive_entry_filetype(struct archive_entry*);
la_int64_t archive_entry_size(struct archive_entry*);

struct archive* archive_write_new(void);
int archive_write_add_filter_none(struct archive*);
int archive_write_set_format_zip(struct archive*);
int archive_write_open_filename(struct archive*, const char*);
int archive_write_open_fd(struct archive*, int);
int archive_write_header(struct archive*, struct archive_entry*);
la_ssize_t archive_write_data(struct archive*, const void*, size_t);
int archive_write_close(struct archive*);
int archive_write_free(struct archive*);

struct archive_entry* archive_entry_new(void);
void archive_entry_free(struct archive_entry*);
void archive_entry_set_pathname_utf8(struct archive_entry*, const char*);
void archive_entry_set_size(struct archive_entry*, la_int64_t);
void archive_entry_set_filetype(struct archive_entry*, unsigned int);
void archive_entry_set_perm(struct archive_entry*, mode_t);
}

enum { ARCHIVE_EOF = 1, ARCHIVE_OK = 0, AE_IFREG = 0100000 };
