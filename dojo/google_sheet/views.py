import json
import datetime
import googleapiclient.discovery
from googleapiclient.errors import HttpError
from google.oauth2 import service_account

from django.shortcuts import render, get_object_or_404, redirect
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils import timezone
from django.contrib import messages
from django.contrib.auth.models import User
from django.contrib.auth.decorators import user_passes_test
from django.core.exceptions import PermissionDenied

from dojo.models import Finding, System_Settings, Test, Engagement, Product, Dojo_User, Note_Type, NoteHistory, Notes
from dojo.forms import GoogleSheetFieldsForm
from dojo.utils import add_breadcrumb

@user_passes_test(lambda u: u.is_superuser)
def configure_google_drive(request):
    fields = Finding._meta.fields
    system_settings=get_object_or_404(System_Settings, id=1)
    revoke_access = False
    if system_settings.credentials :
        revoke_access = True
        column_details = json.loads(system_settings.column_widths.replace("'",'"'))
        initial = {}
        for field in fields:
            initial[field.name]=column_details[field.name][0]
            if column_details[field.name][1] == 0:
                initial['Protect ' + field.name]=False
            else:
                initial['Protect ' + field.name]=True
        initial['drive_folder_ID']=system_settings.drive_folder_ID
        initial['enable_service']=system_settings.enable_google_sheets
        form = GoogleSheetFieldsForm(all_fields=fields, initial=initial, credentials_required=False)
    else:
        form = GoogleSheetFieldsForm(all_fields=fields, credentials_required=True)
    if request.method == 'POST':
        if system_settings.credentials :
            form = GoogleSheetFieldsForm(request.POST, request.FILES, all_fields=fields, credentials_required=False)
        else:
            form = GoogleSheetFieldsForm(request.POST, request.FILES, all_fields=fields, credentials_required=True)

        if request.POST.get('revoke'):
            system_settings.column_widths=""
            system_settings.credentials=""
            system_settings.drive_folder_ID=""
            system_settings.enable_google_sheets=False
            system_settings.save()
            messages.add_message(
                    request,
                    messages.SUCCESS,
                    "Access revoked",
                    extra_tags="alert-success",)
            return HttpResponseRedirect(reverse('dashboard'))

        if request.POST.get('update'):
            if form.is_valid():
                #Create a dictionary object from the uploaded credentials file
                if len(request.FILES) != 0:
                    cred_file = request.FILES['cred_file']
                    cred_byte=cred_file.read()                          #read data from the temporary uploaded file
                    cred_str = cred_byte.decode('utf8')                 #convert bytes object to string
                else:
                    cred_str = system_settings.credentials

                #Get the drive folder ID
                drive_folder_ID = form.cleaned_data['drive_folder_ID']
                validate_inputs = validate_drive_authentication(request, cred_str, drive_folder_ID)

                if validate_inputs :
                    #Create a dictionary of column names and widths
                    column_widths={}
                    for i in fields:
                        column_widths[i.name] = []
                        column_widths[i.name].append(form.cleaned_data[i.name])
                        if form.cleaned_data['Protect ' + i.name]:
                            column_widths[i.name].append(1)
                        else:
                            column_widths[i.name].append(0)

                    system_settings.column_widths=column_widths
                    system_settings.credentials=cred_str
                    system_settings.drive_folder_ID=drive_folder_ID
                    system_settings.enable_google_sheets=form.cleaned_data['enable_service']
                    system_settings.save()
                    return HttpResponseRedirect(reverse('dashboard'))
    add_breadcrumb(title="Google Sheet sync Configuration", top_level=True, request=request)
    return render(request, 'dojo/google_sheet_configuration.html', {
        'name': 'Google Sheet Sync Configuration',
        'metric': False,
        'form':form,
        'revoke_access':revoke_access,
    })


def validate_drive_authentication(request, cred_str, drive_folder_ID):
    SCOPES = ['https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/spreadsheets']
    service_account_info = json.loads(cred_str)
    try:
        #Validate the uploaded credentials file
        credentials = service_account.Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
    except ValueError :
        messages.add_message(
            request,
            messages.ERROR,
            'Invalid credentials file.',
            extra_tags='alert-danger')
        return False
    else:
        sheets_service = googleapiclient.discovery.build('sheets', 'v4', credentials=credentials)
        drive_service = googleapiclient.discovery.build('drive', 'v3', credentials=credentials)
        spreadsheet = {
        'properties': {
            'title': 'Test spreadsheet'
            }
        }
        try:
            #Check the sheets API is enabled or not
            spreadsheet = sheets_service.spreadsheets().create(body=spreadsheet, fields='spreadsheetId').execute()
        except googleapiclient.errors.HttpError:
            messages.add_message(
                request,
                messages.ERROR,
                'Enable the sheets API from the google developer console.',
                extra_tags='alert-danger')
            return False
        else:
            spreadsheetId = spreadsheet.get('spreadsheetId')
            try:
                #Check the drive API is enabled or not
                file = drive_service.files().get(fileId=spreadsheetId, fields='parents').execute() # Retrieve the existing parents to remove
            except googleapiclient.errors.HttpError:
                messages.add_message(
                    request,
                    messages.ERROR,
                    'Enable the drive API from the google developer console.',
                    extra_tags='alert-danger')
                return False
            else:
                previous_parents = ",".join(file.get('parents'))
                folder_id = drive_folder_ID
                try:
                    #Validate the drive folder id and it's permissions
                    file = drive_service.files().update(fileId=spreadsheetId,              # Move the file to the new folder
                                                        addParents=folder_id,
                                                        removeParents=previous_parents,
                                                        fields='id, parents').execute()
                except googleapiclient.errors.HttpError as error:
                    if error.resp.status == 403:
                        messages.add_message(
                            request,
                            messages.ERROR,
                            'Application does not have write access to the given google drive folder',
                            extra_tags='alert-danger')
                    if error.resp.status == 404:
                        messages.add_message(
                            request,
                            messages.ERROR,
                            'Google drive folder ID is invalid',
                            extra_tags='alert-danger')
                    return False
                else:
                    drive_service.files().delete(fileId=spreadsheetId).execute()           # Delete 'test spreadsheet'
                    messages.add_message(
                        request,
                        messages.SUCCESS,
                        "Google drive configuration successful.",
                        extra_tags="alert-success",
                    )
                    return True


@user_passes_test(lambda u: u.is_staff)
def sync_findings(request, tid, spreadsheetId):
    print ('---------------------------------------syncing-----------------------------------')
    test = Test.objects.get(id=tid)
    system_settings = get_object_or_404(System_Settings, id=1)
    service_account_info = json.loads(system_settings.credentials)
    SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
    credentials = service_account.Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
    sheets_service = googleapiclient.discovery.build('sheets', 'v4', credentials=credentials)
    result = sheets_service.spreadsheets().values().get(spreadsheetId=spreadsheetId, range='Sheet1').execute()
    rows = result.get('values', [])
    header_raw = rows[0]
    findings_sheet = rows[1:]
    findings_db = Finding.objects.filter(test=test).order_by('numerical_severity')
    column_details = json.loads(system_settings.column_widths.replace("'",'"'))
    active_note_types = Note_Type.objects.filter(is_active=True)
    note_type_activation = len(active_note_types)

    error_findings = {}
    index_of_active = header_raw.index('active')
    index_of_verified = header_raw.index('verified')
    index_of_duplicate = header_raw.index('duplicate')
    index_of_false_p = header_raw.index('false_p')
    index_of_id = header_raw.index('id')

    for finding_sheet in findings_sheet:
        finding_id = finding_sheet[index_of_id]
        active = finding_sheet[index_of_active]
        verified = finding_sheet[index_of_verified]
        duplicate = finding_sheet[index_of_duplicate]
        false_p = finding_sheet[index_of_false_p]

        if (active == 'TRUE' or verified == 'TRUE') and duplicate == 'TRUE':                     #Check update finding conditions
            error_findings[finding_id] = 'Duplicate findings cannot be verified or active'
        if false_p == 'TRUE' and verified == 'TRUE':
            error_findings[finding_id] = 'False positive findings cannot be verified.'
        else:
            finding_db = findings_db.get(id=finding_id)                                          #Update finding attributes
            finding_notes = finding_db.notes.all()
            for column_name in header_raw:
                if column_name in column_details:
                    if int(column_details[column_name][1])==0 :
                        index_of_column = header_raw.index(column_name)
                        if finding_sheet[index_of_column] == 'TRUE':
                            setattr(finding_db, column_name, True)
                        elif finding_sheet[index_of_column] == 'FALSE':
                            setattr(finding_db, column_name, False)
                        else:
                            setattr(finding_db, column_name, finding_sheet[index_of_column])
                elif column_name[:6]=='[note]' and column_name[-3:]=='_id':                      #Updating notes
                    note_column_name = column_name[:-3]
                    try:
                        index_of_note_column = header_raw.index(note_column_name)
                    except ValueError:
                        pass
                    else:
                        index_of_id_column = header_raw.index(column_name)
                        note_id = finding_sheet[index_of_id_column]
                        note_entry = finding_sheet[index_of_note_column].rstrip()
                        if note_entry != '':
                            if note_id != '':                                                  #If the note is an existing one
                                note_db = finding_notes.get(id=note_id)
                                if note_entry != note_db.entry.rstrip():
                                    note_db.entry = note_entry
                                    note_db.edited = True
                                    note_db.editor = request.user
                                    note_db.edit_time = timezone.now()
                                    history = NoteHistory(data=note_db.entry,
                                                          time=note_db.edit_time,
                                                          current_editor=note_db.editor)
                                    history.save()
                                    note_db.history.add(history)
                                    note_db.save()
                            else:                                                                    #If the note is a newly added one
                                if note_type_activation and note_column_name[7:12] != 'Note_':       #If belongs to a note-type
                                    note_type_name = note_column_name[7:][:-2]
                                    note_type = active_note_types.get(name=note_type_name)
                                    new_note = Notes(note_type=note_type,
                                                    entry=note_entry,
                                                    date=timezone.now(),
                                                    author=request.user)
                                    new_note.save()
                                    history = NoteHistory(data=new_note.entry,
                                                          time=new_note.date,
                                                          current_editor=new_note.author,
                                                          note_type=new_note.note_type)
                                else:
                                    new_note = Notes(entry=note_entry,
                                                    date=timezone.now(),
                                                    author=request.user)
                                    new_note.save()
                                    history = NoteHistory(data=new_note.entry,
                                                          time=new_note.date,
                                                          current_editor=new_note.author)
                                history.save()
                                new_note.history.add(history)
                                finding_db.notes.add(new_note)
            finding_db.last_reviewed = timezone.now()
            finding_db.last_reviewed_by = request.user
            finding_db.save()
    clear_sheet = sheets_service.spreadsheets().values().clear(spreadsheetId=spreadsheetId, range='Sheet1').execute()
    populate_sheet(tid, spreadsheetId, credentials)
    print (error_findings)
    if len(error_findings) > 0 :
        add_breadcrumb(title="Errors", top_level=not len(request.GET), request=request)
        return render(
            request, 'dojo/syncing_errors.html', {
                'error_findings': error_findings
            })
    else:
        messages.add_message(
            request,
            messages.SUCCESS,
            "Google sheet data synced with database",
            extra_tags="alert-success",
        )
        return HttpResponseRedirect(reverse('view_test', args=(tid, )))


@user_passes_test(lambda u: u.is_staff)
def export_to_sheet(request, tid):
    print ('------------------------------------------Creating------------------------------------')
    test = Test.objects.get(id=tid)
    system_settings = get_object_or_404(System_Settings, id=1)
    service_account_info = json.loads(system_settings.credentials)
    SCOPES = ['https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/spreadsheets']
    credentials = service_account.Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
    sheets_service = googleapiclient.discovery.build('sheets', 'v4', credentials=credentials)
    drive_service = googleapiclient.discovery.build('drive', 'v3', credentials=credentials)
    #Create a new spreadsheet
    spreadsheet_name = test.engagement.product.name + "-" + test.engagement.name + "-" + str(test.id)
    spreadsheet = {
    'properties': {
        'title': spreadsheet_name
        }
    }
    spreadsheet = sheets_service.spreadsheets().create(body=spreadsheet, fields='spreadsheetId').execute()
    spreadsheetId = spreadsheet.get('spreadsheetId')
    folder_id = system_settings.drive_folder_ID

    #Move the spreadsheet inside the drive folder
    file = drive_service.files().get(fileId=spreadsheetId, fields='parents').execute()
    previous_parents = ",".join(file.get('parents'))
    file = drive_service.files().update(fileId=spreadsheetId,
                                        addParents=folder_id,
                                        removeParents=previous_parents,
                                        fields='id, parents').execute()
    # user_email = request.user.email
    # drive_service.permissions().create(body={'type':'user', 'role':'writer', 'emailAddress': user_email}, fileId=spreadsheetId).execute()
    populate_sheet(tid, spreadsheetId, credentials)
    messages.add_message(
        request,
        messages.SUCCESS,
        "Finding details successfully exported to google sheet",
        extra_tags="alert-success",
    )
    return HttpResponseRedirect(reverse('view_test', args=(tid, )))


def populate_sheet(tid, spreadsheetId, credentials):
    sheets_service = googleapiclient.discovery.build('sheets', 'v4', credentials=credentials)
    system_settings = get_object_or_404(System_Settings, id=1)
    #Update created spredsheet with finding details
    findings_list = get_findings_list(tid)
    row_count = len(findings_list)
    column_count = len(findings_list[0])
    result = sheets_service.spreadsheets().values().update(spreadsheetId=spreadsheetId,
                                                    range='Sheet1!A1',
                                                    valueInputOption='RAW',
                                                    body = {'values': findings_list}).execute()

    #Format the header row
    body = {
      "requests": [
        {
          "repeatCell": {
            "range": {
              "sheetId": 0,
              "startRowIndex": 0,
              "endRowIndex": 1
            },
            "cell": {
              "userEnteredFormat": {
                "backgroundColor": {
                  "red": 0.0,
                  "green": 0.0,
                  "blue": 0.0
                },
                "horizontalAlignment" : "CENTER",
                "textFormat": {
                  "foregroundColor": {
                    "red": 1.0,
                    "green": 1.0,
                    "blue": 1.0
                  },
                  "fontSize": 12,
                  "bold": True
                }
              }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"
          }
        },
        {
          "updateSheetProperties": {
            "properties": {
              "sheetId": 0,
              "gridProperties": {
                "frozenRowCount": 1
              }
            },
            "fields": "gridProperties.frozenRowCount"
          }
        },
        {
          "addProtectedRange": {
            "protectedRange": {
              "range": {
                "sheetId": 0,
                "startRowIndex": 0,
                "endRowIndex": 1,
                "startColumnIndex": 0,
                "endColumnIndex": column_count,
              },
              # "description": "Protecting total row",
              "warningOnly": False
            }
          }
        }
      ]
    }
    sheets_service.spreadsheets().batchUpdate(spreadsheetId=spreadsheetId, body=body).execute()

    #Format columns with input field widths and protect columns
    result = sheets_service.spreadsheets().values().get(spreadsheetId=spreadsheetId, range='Sheet1!1:1').execute()
    rows = result.get('values', [])
    header_raw = rows[0]
    fields = Finding._meta.fields
    column_details = json.loads(system_settings.column_widths.replace("'",'"'))
    body = {}
    body["requests"]=[]
    for column_name in header_raw:
        index_of_column = header_raw.index(column_name)
        if column_name in column_details:
            if int(column_details[column_name][0])==0:                          #If column width is 0 hide column
                body["requests"].append({
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": 0,
                            "dimension": "COLUMNS",
                            "startIndex": index_of_column,
                            "endIndex": index_of_column+1
                        },
                        "properties": {
                            "hiddenByUser": True,
                        },
                        "fields": "hiddenByUser"
                    }
                })
            else:
                body["requests"].append({                                       #If column width is not 0 adjust column to given width
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": 0,
                            "dimension": "COLUMNS",
                            "startIndex": index_of_column,
                            "endIndex": index_of_column+1
                        },
                        "properties": {
                            "pixelSize": column_details[column_name][0]
                        },
                        "fields": "pixelSize"
                    }
                })
            if column_details[column_name][1] == 1:                             #If protect column is true, protect in sheet
                body["requests"].append({
                  "addProtectedRange": {
                    "protectedRange": {
                      "range": {
                        "sheetId": 0,
                        "startRowIndex": 1,
                        "endRowIndex": row_count,
                        "startColumnIndex": index_of_column,
                        "endColumnIndex": index_of_column+1,
                      },
                      "warningOnly": False
                    }
                  }
                })
            if (fields[index_of_column].get_internal_type()) == "BooleanField":
                body["requests"].append({
                    "setDataValidation": {
                      "range": {
                        "sheetId": 0,
                        "startRowIndex": 1,
                        "endRowIndex": row_count,
                        "startColumnIndex": index_of_column,
                        "endColumnIndex": index_of_column+1,
                      },
                      "rule": {
                        "condition": {
                          "type": "BOOLEAN",
                        },
                        "inputMessage": "Value must be BOOLEAN",
                        "strict": True
                      }
                    }
                  })
            elif (fields[index_of_column].get_internal_type()) == "IntegerField":
                body["requests"].append({
                    "setDataValidation": {
                      "range": {
                        "sheetId": 0,
                        "startRowIndex": 1,
                        "endRowIndex": row_count,
                        "startColumnIndex": index_of_column,
                        "endColumnIndex": index_of_column+1,
                      },
                      "rule": {
                        "condition": {
                          "type": "NUMBER_GREATER",
                          "values": [
                              {
                                "userEnteredValue": "-1"
                              }
                            ]
                        },
                        "inputMessage": "Value must be an integer",
                        "strict": True
                      }
                    }
                  })
            elif (fields[index_of_column].get_internal_type()) == "DateField":
                body["requests"].append({
                    "setDataValidation": {
                      "range": {
                        "sheetId": 0,
                        "startRowIndex": 1,
                        "endRowIndex": row_count,
                        "startColumnIndex": index_of_column,
                        "endColumnIndex": index_of_column+1,
                      },
                      "rule": {
                        "condition": {
                          "type": "DATE_IS_VALID",
                        },
                        "inputMessage": "Value must be a valid date",
                        "strict": True
                      }
                    }
                  })
            elif column_name == "severity":
                body["requests"].append({
                    "setDataValidation": {
                      "range": {
                        "sheetId": 0,
                        "startRowIndex": 1,
                        "endRowIndex": row_count,
                        "startColumnIndex": index_of_column,
                        "endColumnIndex": index_of_column+1,
                      },
                      "rule": {
                        "condition": {
                          "type": "ONE_OF_LIST",
                          "values": [
                              {"userEnteredValue": "Info"},
                              {"userEnteredValue": "Low"},
                              {"userEnteredValue": "Medium"},
                              {"userEnteredValue": "High"},
                              {"userEnteredValue": "Critical"},
                            ]
                        },
                        "inputMessage": "Value must be an one of list",
                        "strict": True
                      }
                    }
                  })
        elif column_name[:6]=='[note]' and column_name[-3:]=='_id':             #Hide and protect note id columns
            body["requests"].append({
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": 0,
                        "dimension": "COLUMNS",
                        "startIndex": index_of_column,
                        "endIndex": index_of_column+1
                    },
                    "properties": {
                        "hiddenByUser": True,
                    },
                    "fields": "hiddenByUser"
                }
            })
            body["requests"].append({
              "addProtectedRange": {
                "protectedRange": {
                  "range": {
                    "sheetId": 0,
                    "startRowIndex": 1,
                    "endRowIndex": row_count,
                    "startColumnIndex": index_of_column,
                    "endColumnIndex": index_of_column+1,
                  },
                  "warningOnly": False
                }
              }
            })

    sheets_service.spreadsheets().batchUpdate(spreadsheetId=spreadsheetId, body=body).execute()


def get_findings_list(tid):
    test = Test.objects.get(id=tid)
    findings = Finding.objects.filter(test=test).order_by('numerical_severity')
    active_note_types = Note_Type.objects.filter(is_active=True).order_by('id')
    note_type_activation = active_note_types.count()

    #Create the header row
    fields = Finding._meta.fields
    findings_list = []
    headings = []
    for i in fields:
        headings.append(i.name)
    findings_list.append(headings)

    #Create finding rows
    for finding in findings:
        finding_details = []
        for field in fields:
            value=eval("finding." + field.name)
            if type(value)==datetime.date or type(value)==Test or type(value)==datetime.datetime:
                var=str(eval("finding." + field.name))
            elif type(value)==User or type(value)==Dojo_User:
                var=value.username
            else:
                var=value
            finding_details.append(var)
        findings_list.append(finding_details)

    #Add notes into the findings_list
    if note_type_activation:
        for note_type in active_note_types:
            max_note_count=1
            if note_type.is_single:
                findings_list[0].append('[note] ' + note_type.name + '_id')
                findings_list[0].append('[note] ' + note_type.name)
            else:
                for finding in findings:
                    note_count = finding.notes.filter(note_type=note_type).count()
                    if max_note_count < note_count :
                        max_note_count=note_count
                for n in range(max_note_count):
                    findings_list[0].append('[note] ' + note_type.name + '_' + str(n+1) + '_id')
                    findings_list[0].append('[note] ' + note_type.name + '_' + str(n+1))
            for f in range(findings.count()):
                finding = findings[f]
                notes = finding.notes.filter(note_type=note_type).order_by('id')
                for note in notes:
                    findings_list[f+1].append(note.id)
                    findings_list[f+1].append(note.entry)
                missing_notes_count = max_note_count - notes.count()
                for i in range(missing_notes_count):
                    findings_list[f+1].append('')
                    findings_list[f+1].append('')
        max_note_count = 0
        for finding in findings:
            note_count = finding.notes.filter(note_type=None).count()
            if max_note_count < note_count:
                max_note_count=note_count
        if max_note_count > 0:
            for i in range(max_note_count):
                findings_list[0].append('[note] ' + "Note_" + str(i+1) + '_id')
                findings_list[0].append('[note] ' + "Note_" + str(i+1))
            for f in range(findings.count()):
                finding = findings[f]
                notes = finding.notes.filter(note_type=None).order_by('id')
                for note in notes:
                    findings_list[f+1].append(note.id)
                    findings_list[f+1].append(note.entry)
                missing_notes_count = max_note_count - notes.count()
                for i in range(missing_notes_count):
                    findings_list[f+1].append('')
                    findings_list[f+1].append('')
    else:
        max_note_count = 1
        for finding in findings:
            note_count = len(finding.notes.all())
            if note_count > max_note_count:
                max_note_count = note_count
        for i in range(max_note_count):
            findings_list[0].append('[note] ' + "Note_" + str(i+1) + '_id')
            findings_list[0].append('[note] ' + "Note_" + str(i+1))
        for f in range(findings.count()):
            finding = findings[f]
            notes = finding.notes.all().order_by('id')
            for note in notes:
                findings_list[f+1].append(note.id)
                findings_list[f+1].append(note.entry)
            missing_notes_count = max_note_count - notes.count()
            for i in range(missing_notes_count):
                findings_list[f+1].append('')
                findings_list[f+1].append('')
    findings_list[0].append('Last column')
    for f in range(findings.count()):
        findings_list[f+1].append('-')
    return findings_list
