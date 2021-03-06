from typing import Dict
from typing import List
from typing import Optional

import numpy as np
from dateutil import parser
from requests import Response

from ...managers.repo_insights_base import ApiClient
from ...managers.repo_insights_base import RepoInsightsManager


class AdoGetProjectWorkItemsClient(ApiClient):
    def _dateDiffBetweenPrSubmissionAndStoryActivation(self, workitem: dict, repo: str, pullRequestSubmitters: Dict[str, Dict[int, str]]) -> Optional[Dict]:
        # Filter for pull requests linked to the workitem
        activatedDate = parser.parse(workitem['fields']['Microsoft.VSTS.Common.ActivatedDate'])
        relationLinkDelimitter = "%2f"
        initialPR: dict = {}
        linkedPullRequests = list(filter(lambda relation: relation['rel'] == "ArtifactLink"
                                         and {"resourceCreatedDate", "name"} <= relation['attributes'].keys()
                                         and {"url"} <= relation.keys()
                                         and relation['attributes']['name'] == 'Pull Request'
                                         and parser.parse(relation['attributes']['resourceCreatedDate']) >= activatedDate,
                                         workitem['relations'] if 'relations' in workitem else []))

        for pr in linkedPullRequests:
            if not bool(initialPR) or parser.parse(initialPR['attributes']['resourceCreatedDate']) > parser.parse(pr['attributes']['resourceCreatedDate']):
                initialPR = pr

        if bool(initialPR):
            # Get the earliest submission date for workitems linked to multiple PRs
            prLinkSplitArray = initialPR['url'].lower().split(relationLinkDelimitter)

            if len(prLinkSplitArray) != 3:
                raise ValueError("The URL for the submitted PR seems to be malformed as the expected format is vstfs:///Git/PullRequestId/%2F=[ProjectId]%2F=[RepoId]%2F=[PullRequestId]")

            prId = int(prLinkSplitArray[2])
            repoId = prLinkSplitArray[1]

            if repoId not in pullRequestSubmitters:
                raise ValueError("Linked Pull Request has an invalid repo ID reference: {}".format(repoId))

            if prId not in pullRequestSubmitters[repoId]:
                raise ValueError("Linked Pull Request has an invalid identifier: {}".format(prId))

            prSubmissionDate = parser.parse(initialPR['attributes']['resourceCreatedDate'])
            timeDelta = divmod((prSubmissionDate - activatedDate).total_seconds(), 60)[0] / 60 / 24

            return {**self.reportableFieldDefaults, **{
                    'contributor': pullRequestSubmitters[repoId][prId],
                    'week': prSubmissionDate.strftime("%V"),
                    'user_story_initial_pr_submission_days': timeDelta,
                    'repo': repo
                    }}

        return None

    def getDeserializedDataset(self, **kwargs) -> List[dict]:
        required_args = {'teamId', 'project', 'repo', 'pullRequestSubmitters'}
        RepoInsightsManager.checkRequiredKwargs(required_args, **kwargs)

        wiQLQuery = "Select [System.Id] From WorkItems Where [System.WorkItemType] = 'User Story' AND [State] <> 'Removed'"
        uri_parameters: Dict[str, str] = {}
        uri_parameters['api-version'] = "6.0"
        project: str = kwargs['project']
        teamId: str = kwargs['teamId']
        repo: str = kwargs['repo']
        pullRequestSubmitters: Dict[str, Dict[int, str]] = kwargs['pullRequestSubmitters']

        resourcePath = "{}/{}/{}/_apis/wit/wiql".format(self.organization, project, teamId)
        return self.DeserializeResponse(self.PostResponse(resourcePath, {"query": wiQLQuery}, uri_parameters), project, repo, pullRequestSubmitters)

    def GetResponse(self, resourcePath: str, uri_parameters: Dict[str, str]) -> Response:
        return self.sendGetRequest(resourcePath, uri_parameters)

    def PostResponse(self, resourcePath: str, json: dict, uri_parameters: Dict[str, str]) -> Response:
        return self.sendPostRequest(resourcePath, json, uri_parameters)

    def DeserializeResponse(self, response: Response, project: str, repo: str, pullRequestSubmitters: Dict[str, Dict[int, str]]) -> List[dict]:
        recordList: List[dict] = []
        jsonResults = response.json()['workItems']

        recordsProcessed = 0
        topElements = 200

        while recordsProcessed < len(jsonResults):
            workitemIds = [str(w['id']) for w in jsonResults[recordsProcessed:topElements+recordsProcessed]]
            recordList += self.GetWorkitemDetails(workitemIds, project)
            recordsProcessed += topElements

        return self.ParseWorkitems(repo, recordList, pullRequestSubmitters)

    def GetWorkitemDetails(self, workItemIds: List[str], project: str) -> List[dict]:
        if len(workItemIds) > 200:
            raise SystemError('The workitems API only supports up to 200 items for a single call.')

        uri_parameters: Dict[str, str] = {}
        uri_parameters['ids'] = ','.join(workItemIds)
        uri_parameters['api-version'] = "6.0"
        uri_parameters['$expand'] = "Relations"

        resourcePath = "{}/{}/_apis/wit/workitems".format(self.organization, project)

        return self.GetResponse(resourcePath, uri_parameters).json()['value']

    def ParseWorkitems(self, repo: str, workitems: List[dict], pullRequestSubmitters: Dict[str, Dict[int, str]]) -> List[dict]:
        recordList = []

        for workitem in workitems:
            recordList.append(
                {**self.reportableFieldDefaults, **{
                    'contributor': workitem['fields']['System.CreatedBy']['displayName'],
                    'week': parser.parse(workitem['fields']['System.CreatedDate']).strftime("%V"),
                    'repo': repo,
                    'user_stories_created': 1
                }})

            if {'Microsoft.VSTS.Common.ActivatedDate', 'System.AssignedTo'} <= set(workitem['fields']) and workitem['fields']['System.State'] != 'New':
                activatedDate: str = workitem['fields']['Microsoft.VSTS.Common.ActivatedDate']
                storyStatus = workitem['fields']['System.State']
                pullRequestSubmissionTimeDeltaFromStoryActiveDate = self._dateDiffBetweenPrSubmissionAndStoryActivation(workitem, repo, pullRequestSubmitters)

                recordList.append(
                    {**self.reportableFieldDefaults, **{
                        'contributor': workitem['fields']['System.AssignedTo']['displayName'],
                        'week': parser.parse(activatedDate).strftime("%V"),
                        'repo': repo,
                        'user_stories_assigned': 1,
                        'user_stories_completed': 1 if storyStatus in ['Closed', 'Resolved'] else 0,
                        'user_story_points_completed': workitem['fields']['Microsoft.VSTS.Scheduling.StoryPoints'] if storyStatus in ['Closed', 'Resolved'] and 'Microsoft.VSTS.Scheduling.StoryPoints' in workitem['fields'] else 0,
                        'user_story_points_assigned': workitem['fields']['Microsoft.VSTS.Scheduling.StoryPoints'] if 'Microsoft.VSTS.Scheduling.StoryPoints' in workitem['fields'] else 0,
                        'user_story_completion_days': RepoInsightsManager.dateStrDiffInDays(workitem['fields']['Microsoft.VSTS.Common.ResolvedDate'], activatedDate) if storyStatus in ['Closed', 'Resolved'] else np.nan
                    }})

                if pullRequestSubmissionTimeDeltaFromStoryActiveDate is not None:
                    recordList.append(pullRequestSubmissionTimeDeltaFromStoryActiveDate)

        return recordList
